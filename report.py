"""One self-contained HTML page per session: what ran, how it ended, and the screenshots taken along the way.

Pure rendering — the caller supplies the record and the screenshot files. Images are embedded as data URIs so the
report can be moved or emailed without carrying a folder of PNGs, and anything unreadable is skipped rather than
turning an audit trail into a traceback.
"""
from __future__ import annotations

import base64
import html
import mimetypes
from pathlib import Path
from typing import Iterable, Mapping

MAX_IMAGES = 12
MAX_IMAGE_BYTES = 3_000_000


def data_uri(path: Path) -> str | None:
    """Base64 data URI for *path*, or ``None`` when it is missing, empty or too large to embed."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            return None
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{payload}"


def render(record: Mapping[str, object], shots: Iterable[Path] = ()) -> str:
    """The whole report as one HTML document.

    Field names come from the run record (``ts``, ``meeting``, ``status``, ``exit_code``, ``seconds``, ``path``,
    ``detail``); labels are fixed so a missing field renders as an empty cell, not a KeyError.
    """
    rows = [
        ("When", record.get("ts")),
        ("Meeting", record.get("meeting") or record.get("entry")),
        ("Scheduled", record.get("scheduled")),
        ("Status", record.get("status")),
        ("Exit code", record.get("exit_code")),
        ("Duration", f"{record.get('seconds')}s" if record.get("seconds") is not None else None),
        ("Entry", record.get("entry")),
        ("Detail", record.get("detail")),
    ]
    body = "\n".join(
        f"      <tr><th>{html.escape(label)}</th><td>{html.escape('' if value is None else str(value))}</td></tr>"
        for label, value in rows
    )
    recording = str(record.get("path") or "")
    recording_html = (
        f'      <p>Recording: <code>{html.escape(recording)}</code></p>'
        if recording
        else "      <p>No recording file.</p>"
    )
    images = []
    for path in list(shots)[:MAX_IMAGES]:
        uri = data_uri(Path(path))
        if uri:
            images.append(
                f'      <figure><img src="{uri}" alt="{html.escape(Path(path).name)}">'
                f"<figcaption>{html.escape(Path(path).name)}</figcaption></figure>"
            )
    gallery = "\n".join(images) or "      <p>No screenshots were taken.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(str(record.get('meeting') or record.get('entry') or 'Recording'))} — run report</title>
  <style>
    body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 60rem; padding: 0 1rem; }}
    table {{ border-collapse: collapse; margin: 1rem 0; }}
    th {{ text-align: left; padding: .25rem 1rem .25rem 0; color: #555; font-weight: 600; }}
    td {{ padding: .25rem 0; }}
    figure {{ margin: 1rem 0; }}
    img {{ max-width: 100%; border: 1px solid #ddd; }}
    figcaption {{ color: #666; font-size: .85em; }}
  </style>
</head>
<body>
  <h1>Run report</h1>
  <table>
{body}
  </table>
{recording_html}
  <h2>Screenshots</h2>
{gallery}
</body>
</html>
"""
