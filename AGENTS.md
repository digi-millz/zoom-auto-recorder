# AGENTS.md

Conventions for anyone (human or agent) changing this repo. `PLAN.md` is the design; this file is the rules of
the road.

## Environment

- Windows only. Python **3.13** at `E:\Programs\Python\Python313`.
- Bare `python` on PATH is the Microsoft Store stub — always use the venv:
  - `.venv\Scripts\python.exe`
  - `.venv\Scripts\python.exe -m unittest discover -s tests -t .`
- Create/refresh: `py -m venv .venv && .venv\Scripts\python.exe -m pip install -r requirements.txt`
- OBS Studio 28+ must be installed with **Tools → WebSocket Server Settings** enabled (port 4455).

## Commands

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t .        # full suite, no Zoom/OBS needed
.venv\Scripts\python.exe main.py status                            # runner pid, next event, OBS reachability
.venv\Scripts\python.exe main.py inspect --url <link> --seconds 20 # window/OCR/capture dump + click decisions
.venv\Scripts\python.exe main.py probe   --url <link>              # join, dismiss dialogs, report (no OBS)
.venv\Scripts\python.exe -m pyflakes *.py tests/*.py               # static check (pip install pyflakes)
```

## Hard rules

1. **Say what you are about to do, then do it.** Before any multi-step change, state the intent and the approach
   in one or two lines ("replacing the blind Enter with an OCR click + verification read; touching `dialogs.py`,
   `watcher.py`, 3 tests"). Then act without narrating each command; if the evidence forces a different plan, say so
   at that point. Never mutate files, services, or the user's desktop without having said what and why.
2. **One runtime.** Python only. No C#/Rust/JS sidecars. OCR ladder is `pywinrt` → `winsdk` → `pytesseract`.
3. **Additive-only CLI.** No existing flag changes meaning. New behaviour gets a new flag or a JSON config key.
4. **Never click blind.** No `keybd_event(VK_RETURN)` on a dialog, no fixed-fraction clicks. Every click comes
   from a located label (UIA name or OCR word box), and every click is followed by a verification read.
   Denylisted labels (`Cancel`, `Decline`, `Sign In`, `Switch Account`, `Leave`, `End Meeting`, `Remove`) lose to
   the allowlist, always. No match → no click → `needs_attention`.
5. **Behaviour is data.** Dialog labels live in `rules.json`, not in code, so a Zoom relabel doesn't need a
   release.
6. **Pure where possible.** Anything testable without Windows I/O is a pure function (`dialogs.pick`,
   `recurrence.next_start_local`, `window_manager.select_meeting_window`). Only the thin edge touches Win32.
7. **Keep the suite green.** Add tests for new pure logic; do not weaken existing assertions, and delete tests that
   only pin removed code.
8. **Failures are states, not exceptions.** `missed`, `needs_attention`, exit codes 0–8. A silent no-file is a
   bug; a logged, alarmed no-file is acceptable.

## Windows gotchas (already bitten)

- HWND is 64-bit: every `ctypes` function needs `argtypes`/`restype` set, never a bare `int`.
- The process must be PerMonitorV2 DPI-aware **before** any capture or click, or coordinates are wrong at any
  scaling ≠ 100 %.
- OCR boxes are pixels inside the captured bitmap; add the window's screen origin before clicking.
- Minimised windows capture black; locked sessions capture black. Check `is_black()` and abort with a message
  rather than producing an unusable file.
- `Zoom.exe` outlives the meeting — stop on the meeting **HWND**, never on process exit.
- OBS: `StartRecord`/`StopRecord` must be confirmed by polling state; `StopRecord`'s `outputPath` can arrive via
  the `RecordStateChanged` event, not the response.

## Layout

| File | Role |
| --- | --- |
| `cli.py` | Click entry point, runner, session flow, exit codes |
| `store.py` | `schedule.json` (entries + settings), recurrence roll-forward, lock, stop marker |
| `scheduler.py` | date parsing, countdown |
| `recurrence.py` | RRULE + timezone maths (the only place occurrences are computed) |
| `window_manager.py` | zoommtg launch, window discovery/selection, maximise, `click_at`, `type_text` |
| `obs_controller.py` | obs-websocket v5 client, record control, directory override, crash-marker guard |
| `webui.py`, `ui.html` | localhost calendar/control panel (token-guarded, single instance) |
| `ics.py` | `.ics` import |
| `dialogs.py` | pure rule matching: OCR words → the click to make (no Win32) |
| `watcher.py` | session loop: capture → read → decide → click → verify → alert |
| `ocr.py` | `Windows.Media.Ocr` words with pixel boxes |
| `win_capture.py` | window frames (`PrintWindow`), screen origin, black-frame check |
| `scene.py` | OBS scene bootstrap: window capture, app audio, global-audio mute |
| `postprocess.py` | remux, rename, `ffprobe` verification |
| `report.py` | per-run HTML report: record + screenshots → one self-contained file (pure) |
| `teach.py` | teach mode: unmatched-dialog screenshots → validated `rules.json` rule (pure) |
| `rules.json` | dialog labels and limits (data, not code) |
| `tests/` | unittest; live-meeting evidence in `docs/spikes.md` |

## Workflow

Work phase by phase from `PLAN.md` §5. A phase is done when its **acceptance** check has actually been run and
its output quoted — not when the code looks right. Anything needing a live meeting (P0 fixtures, P3 live join,
P4 real recording) is marked blocked until a link + OBS are available, and the code must fail loudly, not
silently, when that verification hasn't happened.
