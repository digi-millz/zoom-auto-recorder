"""Dialog rules: decide *which* word to click, from OCR words. Pure, no Windows calls.

`pick()` is the whole decision surface: given the words OCR found in one Zoom window and the rules file, it
returns a click target or ``None``. Nothing here touches Win32, so every rule, typo tolerance and guardrail is
unit-testable from recorded word lists (see ``tests/test_dialogs.py``).

Invariants
----------
* The denylist beats the allowlist: a candidate whose text matches a deny phrase is never clicked.
* A rule fires at most ``max_fires`` times per run (the caller owns the counter).
* No match returns ``None`` — the caller records ``needs_attention`` instead of guessing.
* Exact matches outrank fuzzy ones; ties break top-to-bottom, then left-to-right.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from ocr import Word

DEFAULT_THRESHOLD = 0.85
DEFAULT_DENY = (
    "Cancel",
    "Decline",
    "Sign In",
    "Switch Account",
    "Leave",
    "Leave Meeting",
    "End Meeting",
    "Remove",
    "Exit",
)

# Window class -> rule scope, so a rule only runs against the window it was written for.
SCOPE_CLASSES: dict[str, frozenset[str]] = {
    "dialog": frozenset({"zRichMsgBoxClass", "ConfPopupTopClass"}),
    "prejoin": frozenset(
        {"VideoPreviewWndClass", "zWaitingMeetingIDWndClass", "zSimpleMessageDialogClass"}
    ),
    # The waiting room is a normal state, not an unhandled dialog: recording continues there until admitted.
    "meeting": frozenset(
        {
            "ZPMeetingMainWndClass",
            "ConfMultiTabContentWndClass",
            "ZPContentViewWndClass",
            "zWaitingRoomWndClass",
        }
    ),
    # Zoom windows that never carry a clickable decision: skipped before any capture/OCR work.
    # zLeaveWndClass is "End meeting or leave meeting?" — clicked only by an explicit auto-leave step, never by a
    # rule, because guessing between "Leave" and "End meeting" ends everyone's call.
    "other": frozenset(
        {
            "ZPPTMainFrmWndClassEx",
            "ZPToolBarParentWndClass",
            "zLeaveWndClass",
            "zoom_acc_notify_wnd",
            "zoom_pt_acc_notify_wnd",
            "zoom_pt_notification_app_bar_wnd",
            # "This meeting is being recorded." — a 1250x65 banner with no button.
            "ZDisclaimerBannerWndClass",
            "ZoomShadowFrameClass",
            # "Note-taking is available" toast: its only button opens a panel, so clicking it is wrong.
            "CVoiceActivityDetectedDlgCls",
            # Child panel of the prejoin window ("audio" + device names); the parent carries the Join button.
            "VideoPreviewFitPanelClass",
            # Zoom's stage/animation surface — no decision ever lives here.
            "Duilib_Anim_StageWindowClassName",
        }
    ),
}

# Per-instance class names that can never hold a decision: ATL windows carry a module address in the class
# ("ATL:00007FFFB743AD70", title "VideoPortContainer").
_IGNORED_PREFIXES = ("ATL:",)


def scope_for_class(cls: str, default: str = "dialog") -> str:
    """Which rule scope applies to a window class (``dialog``/``prejoin``/``meeting``/``other``).

    Unknown classes default to ``dialog`` so a Zoom rename cannot hide a consent prompt; only classes known to be
    benign are excluded as ``other``.
    """
    if cls.startswith(_IGNORED_PREFIXES):
        return "other"
    for scope, classes in SCOPE_CLASSES.items():
        if cls in classes:
            return scope
    return default


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return " ".join(_NON_ALNUM.sub(" ", text.lower()).split())


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    @classmethod
    def union(cls, boxes: Iterable["Box"]) -> "Box":
        boxes = list(boxes)
        x = min(b.x for b in boxes)
        y = min(b.y for b in boxes)
        right = max(b.x + b.width for b in boxes)
        bottom = max(b.y + b.height for b in boxes)
        return cls(x, y, right - x, bottom - y)


@dataclass(frozen=True)
class Line:
    """One OCR line: its words in reading order and their bounding box."""

    words: tuple[Word, ...]
    box: Box
    index: int = 0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def lines_of(words: Sequence[Word]) -> list[Line]:
    """Group OCR words into lines (by their ``line`` index), top-to-bottom, left-to-right."""
    grouped: dict[int, list[Word]] = {}
    for word in words:
        grouped.setdefault(word.line, []).append(word)
    result: list[Line] = []
    for index in sorted(grouped):
        ordered = tuple(sorted(grouped[index], key=lambda w: w.x))
        box = Box.union(Box(w.x, w.y, w.width, w.height) for w in ordered)
        result.append(Line(words=ordered, box=box, index=index))
    return result


@dataclass(frozen=True)
class Rule:
    name: str
    labels: tuple[str, ...]
    scope: str = "dialog"
    exclude: tuple[str, ...] = ()
    max_fires: int = 3
    threshold: float = DEFAULT_THRESHOLD


@dataclass(frozen=True)
class Rules:
    rules: tuple[Rule, ...]
    deny: tuple[str, ...] = DEFAULT_DENY

    def for_scope(self, scope: str) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.scope in ("any", scope))

    def without(self, *names: str) -> "Rules":
        """A copy with some rules disabled — how a CLI flag turns a behaviour off without editing rules.json."""
        disabled = {str(name) for name in names}
        if not disabled:
            return self
        return Rules(rules=tuple(r for r in self.rules if r.name not in disabled), deny=self.deny)


@dataclass(frozen=True)
class Click:
    """A decision: click at ``(x, y)`` (frame pixels) for ``label``."""

    rule: str
    label: str
    matched: str
    x: int
    y: int
    box: Box
    score: float
    line: int


@dataclass
class ClickLog:
    """Per-run firing counters and the audit trail."""

    fired: dict[str, int] = field(default_factory=dict)
    clicks: list[Click] = field(default_factory=list)

    def count(self, rule: str) -> int:
        return self.fired.get(rule, 0)

    def record(self, click: Click) -> None:
        self.fired[click.rule] = self.count(click.rule) + 1
        self.clicks.append(click)


def load_rules(path: str | Path | None = None) -> Rules:
    """Load ``rules.json``; a missing/broken file falls back to :data:`DEFAULT_RULES`."""
    if path is None:
        path = Path(__file__).with_name("rules.json")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_RULES
    rules = tuple(
        Rule(
            name=str(item["name"]),
            labels=tuple(str(v) for v in item["match"]["text"]),
            scope=str(item.get("scope", "dialog")),
            exclude=tuple(str(v) for v in item["match"].get("exclude", ())),
            max_fires=int(item.get("max_fires", 3)),
            threshold=float(item.get("threshold", DEFAULT_THRESHOLD)),
        )
        for item in data.get("rules", ())
    )
    deny = tuple(str(v) for v in data.get("deny", DEFAULT_DENY))
    return Rules(rules=rules or DEFAULT_RULES.rules, deny=deny)


def _score(label: str, span: str) -> float:
    """Exact match = 1.0; otherwise a fuzzy ratio capped below exactness."""
    if span == label:
        return 1.0
    return difflib.SequenceMatcher(None, label, span).ratio() * 0.99


def _span_candidates(words: Sequence[Word], label_words: int) -> Iterable[tuple[Word, ...]]:
    """Contiguous word spans of 1..label_words+2 words (OCR splits/merges adjacent labels)."""
    longest = min(len(words), max(1, label_words + 2))
    for size in range(1, longest + 1):
        for start in range(len(words) - size + 1):
            yield tuple(words[start : start + size])


def _best_span(words: Sequence[Word], label: str) -> tuple[float, tuple[Word, ...]] | None:
    """Best-matching word span for ``label`` inside one line."""
    wanted = normalize(label)
    if not wanted:
        return None
    label_words = max(1, len(wanted.split()))
    best: tuple[float, tuple[Word, ...]] | None = None
    for span in _span_candidates(words, label_words):
        score = _score(wanted, normalize(" ".join(w.text for w in span)))
        if best is None or score > best[0]:
            best = (score, span)
    return best


def _denied_by(text: str, rule: Rule, deny: Sequence[str], threshold: float) -> bool:
    """True when the candidate text is itself a deny/exclude phrase."""
    candidate = normalize(text)
    for phrase in (*deny, *rule.exclude):
        wanted = normalize(phrase)
        if not wanted:
            continue
        if candidate == wanted:
            return True
        if difflib.SequenceMatcher(None, wanted, candidate).ratio() >= threshold:
            return True
    return False


def pick(
    words: Sequence[Word],
    rules: Rules,
    scope: str = "dialog",
    log: ClickLog | None = None,
) -> Click | None:
    """Choose the next click for the window that produced ``words``.

    ``scope`` is the window role (``dialog``/``prejoin``/``meeting``). Rules run in file order; the first rule
    with an eligible, non-denied match wins.
    """
    if not words:
        return None
    parsed = lines_of(words)
    for rule in rules.for_scope(scope):
        if log is not None and log.count(rule.name) >= rule.max_fires:
            continue
        # Rank by: exactness, then how *button-like* the line is, then position.
        # A consent dialog says `By clicking "Agree" and continuing…` in a paragraph and puts the actual button
        # alone at the bottom — both match the label exactly, so position and line length decide.
        best: tuple[tuple[float, int, int], Line, tuple[Word, ...]] | None = None
        for line in parsed:
            if _line_excluded(line, rule, rule.threshold):
                continue
            for label in rule.labels:
                found = _best_span(line.words, label)
                if found is None:
                    continue
                score, span = found
                if score < rule.threshold:
                    continue
                span_text = " ".join(w.text for w in span)
                # An explicit rule label outranks the denylist: "leave-confirm" names "Leave Meeting" precisely
                # because it means it. Fuzzy near-misses still lose to the denylist.
                explicit = normalize(span_text) in {
                    normalize(label) for label in rule.labels
                }
                if not explicit and _denied_by(span_text, rule, rules.deny, rule.threshold):
                    continue
                key = (round(score, 3), -len(line.words), line.box.y)
                if best is None or key > best[0]:
                    best = (key, line, span)
        if best is None:
            continue
        _key, line, span = best
        score = _key[0]
        box = Box.union(Box(w.x, w.y, w.width, w.height) for w in span)
        x, y = box.center
        return Click(
            rule=rule.name,
            label=" ".join(w.text for w in span),
            matched=" ".join(rule.labels),
            x=x,
            y=y,
            box=box,
            score=score,
            line=line.index,
        )
    return None




def _line_excluded(line: Line, rule: Rule, threshold: float) -> bool:
    """True when the whole line is a different control than this rule targets.

    Merged OCR lines are the reason this exists: if a prejoin line reads "Join with Computer Audio", the word
    "Join" inside it is the audio dropdown, not the primary button — clicking that fragment is wrong. Only
    ``rule.exclude`` is checked here; the global denylist stays span-level so "Acknowledge   Cancel" on one line
    still lets the consent rule click Acknowledge.
    """
    text = normalize(line.text)
    if not text:
        return True
    for phrase in rule.exclude:
        wanted = normalize(phrase)
        if not wanted:
            continue
        if wanted in text:
            return True
        if difflib.SequenceMatcher(None, wanted, text).ratio() >= threshold:
            return True
    return False


def signature(words: Sequence[Word]) -> str:
    """Fingerprint of a window's text, to prove a click changed the dialog."""
    return "|".join(normalize(line.text) for line in lines_of(words))


DEFAULT_RULES = Rules(
    rules=(
        # Zoom is still in another meeting: "Join new meeting? … Leave & join". This is the only rule that leaves
        # a call, so it comes first, fires once, and is scoped to the prejoin dialog. Remove it from rules.json to
        # refuse the switch and let the run raise needs_attention instead.
        Rule(
            name="leave-and-join",
            labels=("Leave & join", "Leave and join"),
            scope="prejoin",
            max_fires=1,
        ),

        Rule(
            name="consent",
            labels=("Acknowledge", "Continue", "Got it", "I Agree", "Agree", "OK"),
            scope="dialog",
            max_fires=3,
        ),
        Rule(
            name="join-audio",
            labels=("Join with Computer Audio", "Join Audio by Computer", "Use Computer Audio"),
            scope="any",
            max_fires=2,
        ),
        Rule(
            name="prejoin",
            labels=("Join meeting", "Join"),
            scope="prejoin",
            # "Join a Meeting" is deliberately NOT excluded: it fuzzy-matches the "Join meeting" button we want.
            exclude=("Join with Computer Audio", "Join from browser"),
            max_fires=2,
        ),
        Rule(
            name="leave-confirm",
            labels=("Leave Meeting",),
            scope="meeting",
            max_fires=1,
        ),
    ),
    deny=DEFAULT_DENY,
)
