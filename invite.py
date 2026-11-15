"""Parse a pasted Zoom invitation into a join link and passcode.

People paste the whole invite, not a link:

    Join Zoom Meeting
    https://us06web.zoom.us/j/81234567890?pwd=abcDEF123

    Meeting ID: 812 3456 7890
    Passcode: abc123

Zoom shows the meeting ID in space-separated groups and sometimes omits it from the line, so both shapes are handled.
The canonical output is a URL with ``?pwd=`` when a passcode is known: that is what the ``zoommtg://`` handler needs,
and it means the recorder never has to type a passcode into a dialog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

_JOIN_URL = re.compile(r"https?://[a-z0-9.-]*zoom\.(?:us|com)/(?:j|w|s)/(\d{9,11})[^\s<>\"']*", re.I)
_MEETING_ID = re.compile(r"meeting\s*id\s*[:：]?\s*((?:\d[\s-]?){9,13})", re.I)
_PASSCODE = re.compile(r"pass\s*code\s*[:：]?\s*([A-Za-z0-9._@-]{1,32})", re.I)
_BARE_ID = re.compile(r"(?<![\d/])(\d{9,11})(?![\d/])")


@dataclass(frozen=True)
class Invite:
    """What we could understand about an invitation."""

    meeting_id: str | None = None
    passcode: str | None = None
    host: str = "zoom.us"

    @property
    def url(self) -> str | None:
        """Canonical join URL, with ``?pwd=`` when a passcode is known."""
        if not self.meeting_id:
            return None
        query = f"?pwd={self.passcode}" if self.passcode else ""
        return f"https://{self.host}/j/{self.meeting_id}{query}"

    def zoommtg(self) -> str | None:
        """The protocol URL the desktop client opens (no passcode typing needed)."""
        if not self.meeting_id:
            return None
        parts = ["zoommtg://zoom.us/join?action=join", f"confno={self.meeting_id}"]
        if self.passcode:
            parts.append(f"pwd={self.passcode}")
        return "&".join(parts)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def parse(text: str) -> Invite:
    """Understand a link, an invite block, or a bare meeting ID. Never raises."""
    raw = (text or "").strip()
    if not raw:
        return Invite()

    host = "zoom.us"
    meeting_id: str | None = None
    passcode: str | None = None

    match = _JOIN_URL.search(raw)
    if match:
        meeting_id = match.group(1)
        parsed = urlparse(match.group(0))
        if parsed.hostname:
            host = parsed.hostname
        pwd = parse_qs(parsed.query).get("pwd") or []
        if pwd and pwd[0]:
            passcode = pwd[0]

    if meeting_id is None:
        labelled = _MEETING_ID.search(raw)
        if labelled:
            candidate = _digits(labelled.group(1))
            if 9 <= len(candidate) <= 11:
                meeting_id = candidate
    if meeting_id is None:
        # A bare ID (the UI's "meeting ID only" case), but not one inside a longer number or a path.
        bare = _BARE_ID.search(raw)
        if bare:
            meeting_id = bare.group(1)

    if passcode is None:
        found = _PASSCODE.search(raw)
        if found:
            passcode = found.group(1)

    return Invite(meeting_id=meeting_id, passcode=passcode, host=host)


def same_meeting(left: str, right: str) -> bool:
    """True when two pieces of text name the same Zoom meeting.

    ``https://us06web.zoom.us/j/81234567890`` and ``Meeting ID: 812 3456 7890`` are the same meeting on different
    hosts, so URL string equality is the wrong test for duplicates.
    """
    first, second = parse(left), parse(right)
    if first.meeting_id is None or second.meeting_id is None:
        return (left or "").strip().casefold() == (right or "").strip().casefold()
    return first.meeting_id == second.meeting_id


def with_passcode(url: str, passcode: str | None) -> str:
    """Add or replace ``?pwd=`` on a join URL (used when the passcode came from a flag, not the invite)."""
    if not passcode:
        return url
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    query = parse_qs(parsed.query)
    query["pwd"] = [passcode]
    rebuilt = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    return rebuilt
