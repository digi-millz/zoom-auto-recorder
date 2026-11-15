"""Defaults shared by the CLI, the UI and the runner.

One place, so the value the UI shows and the value the runner uses cannot drift apart.
"""

from __future__ import annotations

# The UI's pre-selected timezone.
DEFAULT_TZ = "America/New_York"

# There is deliberately no built-in Zoom email: an address in source code is both a privacy leak and a guess about
# who is recording. Resolution order at run time is
#   entry.email  →  settings.email (the UI's "Zoom email" field)  →  zoom_account.discover()  →  nothing.
# With nothing configured the recorder still joins guest meetings; if Zoom asks for an email it raises
# needs_attention rather than typing someone else's address.
