"""Teach mode: turn a screenshot of an unmatched dialog into a rule.

The recorder never guesses a click (see AGENTS.md rule 4), so a dialog it cannot read becomes `needs_attention`
plus a screenshot. This module closes that loop offline: list those screenshots, let the user name the button they
recognise, and append a scoped rule to `rules.json` — validated against the denylist, never overwriting a rule that
already matches the label.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCOPES = ("dialog", "prejoin", "meeting", "any")
SHOT_PATTERN = re.compile(r"^\d{6}_[0-9a-f]+_before_(?P<cls>[A-Za-z0-9_]+)\.png$", re.IGNORECASE)
SESSION_PATTERN = re.compile(r"^\d{8}-\d{6}$")


@dataclass(frozen=True)
class Shot:
    """One `before` screenshot of a dialog the watcher could not handle."""

    session: str
    cls: str
    path: Path

    @property
    def url(self) -> str:
        return f"/api/shots/{self.session}/{self.path.name}"


def class_name(filename: str) -> str | None:
    """Window class from a watcher screenshot name, or ``None`` for anything else."""
    match = SHOT_PATTERN.match(filename)
    return match.group("cls") if match else None


def collect_shots(shots_root: str | Path, sessions: int = 5) -> list[Shot]:
    """One `before` shot per window class, from the newest sessions.

    A class repeats on every retry and every session; the teacher only needs to name it once, so the newest
    screenshot of each class wins and the list stays short.
    """
    root = Path(shots_root).expanduser()
    if not root.is_dir():
        return []
    directories = sorted(
        (entry for entry in root.iterdir() if entry.is_dir() and SESSION_PATTERN.match(entry.name)),
        key=lambda entry: entry.name,
        reverse=True,
    )[: max(sessions, 0)]
    seen: set[str] = set()
    shots: list[Shot] = []
    for directory in directories:
        for image in sorted(directory.glob("*.png"), reverse=True):
            cls = class_name(image.name)
            if cls is None or cls in seen:
                continue
            seen.add(cls)
            shots.append(Shot(session=directory.name, cls=cls, path=image))
    return shots


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-") or "label"


def draft_rule(deny: Iterable[str], cls: str, label: str, scope: str) -> dict:
    """Build the rule a teacher asked for, or raise ``ValueError`` explaining why it is unsafe.

    The denylist wins, as everywhere else: a label that appears in it can never become a taught rule.
    """
    label = (label or "").strip()
    cls = (cls or "").strip()
    if not label:
        raise ValueError("the button label is required")
    if not cls:
        raise ValueError("the window class is required")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    blocked = {entry.strip().lower() for entry in deny}
    if label.lower() in blocked:
        raise ValueError(f"{label!r} is on the denylist; refusing to teach it")
    return {
        "name": f"taught-{_slug(cls)}-{_slug(label)}",
        "scope": scope,
        "match": {"text": [label]},
        "action": {"kind": "clickText"},
        "max_fires": 1,
    }


def load(path: str | Path) -> dict:
    """The rules file as a dict; a missing file yields the minimal shell."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("deny", [])
    data.setdefault("rules", [])
    return data


def add_rule(
    path: str | Path, *, cls: str, label: str, scope: str = "dialog"
) -> dict:
    """Validate and append one taught rule; returns it. Raises ``ValueError`` and leaves the file untouched."""
    path = Path(path)
    data = load(path)
    rule = draft_rule(data.get("deny") or [], cls, label, scope)
    wanted = rule["match"]["text"][0].lower()
    for existing in data["rules"]:
        if not isinstance(existing, dict):
            continue
        texts = (existing.get("match") or {}).get("text") or []
        if any(str(text).strip().lower() == wanted for text in texts):
            raise ValueError(f"{rule['match']['text'][0]!r} is already handled by rule {existing.get('name')!r}")
    data["rules"].append(rule)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    return rule
