"""Optional webhook notification so a failed or interesting run is not silent.

Deliberately tiny and dependency-free: one HTTP POST, one retry, five-second timeout, and **never** raises into a
recording. The body carries both ``text`` (Slack, Google Chat) and ``content`` (Discord) with the same message, plus
machine-readable fields, so the same URL works with any of them.

Unconfigured (empty URL) means "do nothing" — the feature is opt-in.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

TIMEOUT = 5.0

DONE = "done"
FAILED = "failed"
NEEDS_ATTENTION = "needs_attention"
MISSED = "missed"


@dataclass(frozen=True)
class Event:
    """What happened, in the terms a human reading a chat message cares about."""

    kind: str
    meeting: str = ""
    path: str | None = None
    exit_code: int | None = None
    detail: str = ""

    @property
    def icon(self) -> str:
        return {DONE: "✅", FAILED: "❌", NEEDS_ATTENTION: "⚠️", MISSED: "⏭"}.get(self.kind, "•")

    def text(self) -> str:
        who = self.meeting or "meeting"
        if self.kind == DONE:
            head = f"{self.icon} recording finished: {who}"
            if self.path:
                head += f"\n{self.path}"
        elif self.kind == FAILED:
            code = f" (exit {self.exit_code})" if self.exit_code not in (None, 0) else ""
            head = f"{self.icon} recording failed{code}: {who}"
        elif self.kind == NEEDS_ATTENTION:
            head = f"{self.icon} needs attention: {who}"
        elif self.kind == MISSED:
            head = f"{self.icon} missed: {who} (started while the runner was down)"
        else:
            head = f"{self.icon} {self.kind}: {who}"
        if self.detail:
            head += f"\n{self.detail.strip()}"
        return head

    def payload(self) -> dict:
        return {
            "text": self.text(),
            "content": self.text(),  # Discord
            "event": self.kind,
            "meeting": self.meeting,
            "path": self.path,
            "exit_code": self.exit_code,
            "detail": self.detail,
        }


def send(url: str | None, event: Event, *, timeout: float = TIMEOUT) -> bool:
    """POST *event* to *url*. Returns whether it landed; never raises.

    One retry, because the common failure is a transient DNS/socket hiccup — and a notification is not worth
    failing a recording over.
    """
    if not url or not url.strip():
        return False
    body = json.dumps(event.payload()).encode("utf-8")
    request = urllib.request.Request(
        url.strip(),
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "zoom-auto-recorder"},
        method="POST",
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    log.debug("notified %s for %s", event.kind, event.meeting or "meeting")
                    return True
                log.warning("notification endpoint replied %s", response.status)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning(
                "notification attempt %d failed: %s",
                attempt,
                getattr(exc, "reason", exc),
            )
        except Exception as exc:  # noqa: BLE001 - a notifier must never break a run
            log.warning("notification attempt %d failed unexpectedly: %s", attempt, exc)
    return False
