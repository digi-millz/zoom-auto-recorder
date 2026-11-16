# AGENTS.md

Conventions for anyone (human or agent) changing this repo. `PLAN.md` is the design; this file is the rules of
the road.

## Environment

- Windows only. Python **3.13**, anywhere on the machine — the repo names no interpreter path. `py -3.13` is
  the per-machine launcher that finds it.
- Bare `python` on PATH is the Microsoft Store stub — always use the venv:
  - `.venv\Scripts\python.exe`
  - `.venv\Scripts\python.exe -m unittest discover -s tests -t .`
- Create/refresh on any machine: `py -3.13 -m venv .venv && .venv\Scripts\python.exe -m pip install -r requirements.txt`
- Everything else is relative: `.venv\` (gitignored) and the default paths `./schedule.json` / `./recordings`
  (`cli.DEFAULT_SCHEDULE`, `cli.DEFAULT_OUTPUT`) resolve against the working directory, so run commands from the
  repo root and the same checkout works on every machine.
- OBS Studio 28+ must be installed with **Tools → WebSocket Server Settings** enabled (port 4455).
- `requirements.txt` also carries the OCR ladder (`winrt-*`, Windows OCR). Without it the watcher reads **nothing**
  and every join stalls at Zoom's "Enter your email and name" prompt while the log repeats
  `OCR failed: No module named 'winrt'`. After a fresh venv, `.venv\Scripts\python.exe -c "import ocr; print(ocr.available())"`
  must print `True`.
- **Machine-specific facts belong in `MACHINE.md`, never in a tracked doc.** Interpreter path, GPU, monitors, DPI,
  OBS/Zoom install paths, the recording folder, the zoom email, what Task Scheduler has registered — one checkout per
  machine, so anything true of *this* box and false of another goes there (it is gitignored). Tracked docs state
  requirements and behaviour; measured evidence is dated in `docs/spikes.md` and says which machine it came from.

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
8. **Failures are states, not exceptions.** `missed`, `needs_attention`, exit codes 0–9. A silent no-file is a
   bug; a logged, alarmed no-file is acceptable.
9. **Close what you open.** A test that launches Zoom, starts a recording, opens a browser tab or a probe must end
   with that window closed, that process gone and its artifacts deleted — in a `finally`, before reporting, not
   "later". Leave running only what the product needs (the runner, the UI, OBS) and say which those are. A rehearsal's
   waiting room left on the user's desktop is a bug, and a 60 s test take left in `recordings/` is litter.

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
| `notify.py` | opt-in webhook (done/failed/needs_attention/missed), never blocks a recording |
| `invite.py` | parse a pasted invitation (link, spaced meeting ID, passcode) and compare meeting identity |
| `defaults.py` | values shared by CLI, UI and runner (the UI's default zone) |
| `zoom_account.py` | reads the Zoom email from the client's own config (never in source) |
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
| `docs/` | `spikes.md` (live-meeting evidence), `live-acceptance.md` (the checks that need a meeting) |
| `MACHINE.md` | this checkout's facts — interpreter path, GPU, monitors, install paths, recording folder (gitignored) |
| `tests/` | unittest (no Zoom/OBS required) |

## Workflow

Work phase by phase from `PLAN.md` §5. A phase is done when its **acceptance** check has actually been run and
its output quoted — not when the code looks right. Anything needing a live meeting (P0 fixtures, P3 live join,
P4 real recording) is marked blocked until a link + OBS are available, and the code must fail loudly, not
silently, when that verification hasn't happened.
