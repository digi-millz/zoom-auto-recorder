"""Best-effort discovery of the signed-in Zoom account's email.

Zoom does **not** expose the account address in a stable local file: on this machine the client keeps it in
encrypted config / the webview's SQLite caches, where the only plain-text addresses belong to *other* people
(chat contacts, avatars). Scraping those would silently put a stranger's address in a prejoin form, so this module
only reads explicit account-config files and returns ``None`` when it cannot be sure.

Resolution order used by the app (there is no built-in address in the source):

1. the meeting's own ``email`` (set when the event was added),
2. ``settings.email`` in ``schedule.json`` (the UI's "Zoom email" field — set it once),
3. :func:`discover` — a config file that spells the account out,
4. nothing: the prompt is left alone and the run reports ``needs_attention``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Files a Zoom build may keep account facts in. Absent ones are simply skipped.
CONFIG_CANDIDATES = (
    ("Zoom", "data", "zoomus.conf"),
    ("Zoom", "data", "zoom.us.conf"),
    ("Zoom", "data", "config", "account.conf"),
    ("Zoom", "data", "config", "zoomus.conf"),
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Exact keys only: "SupportEmail" or "Nickname" must never be mistaken for the account owner.
_KEYS = frozenset({"useremail", "email", "accountemail", "loginemail", "signinemail"})


def _candidate_value(text: str) -> str | None:
    """First ``key=value`` line whose key names an account email and whose value looks like one."""
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower().lstrip("[").rstrip("]").replace("_", "")
        if key not in _KEYS:
            continue
        match = _EMAIL.search(value)
        if match is None:
            continue
        address = match.group(0)
        if address.lower().endswith("@xmpp.zoom.us"):  # internal JID, never a mailbox
            continue
        return address
    return None


def discover(appdata: str | Path | None = None) -> str | None:
    """The account email from an explicit config file, or ``None`` when the client does not publish one."""
    base = Path(appdata) if appdata is not None else Path(os.environ.get("APPDATA", ""))
    for parts in CONFIG_CANDIDATES:
        path = base.joinpath(*parts)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        address = _candidate_value(text)
        if address:
            log.debug("Zoom account email taken from %s", path)
            return address
    return None
