# zoom-auto-recorder

Schedules a Zoom meeting, opens it with the Zoom desktop client, maximises the
meeting window, starts an OBS Studio recording over `obs-websocket` v5, and
stops that recording when the meeting window closes.

Windows only — it drives the Win32 window APIs and the Zoom desktop app.

```
zoom-auto-recorder/
├── main.py              # Entry point (python main.py record ...)
├── cli.py               # Click command, orchestration, exit codes, cleanup
├── scheduler.py         # datetime maths + live countdown
├── window_manager.py    # Win32 window discovery / maximise, zoommtg:// launch
├── obs_controller.py    # OBS WebSocket v5 control + standalone launch fallback
├── store.py             # JSON schedule, recurrence roll-forward, lock, stop marker
├── webui.py             # localhost JSON API + runner control (token-guarded)
├── ui.html              # calendar page (no external assets)
├── ics.py               # minimal .ics -> schedule importer
├── requirements.txt
├── tests/               # unittest suite (no Zoom/OBS required)
└── README.md
```

## Requirements

- Windows 10/11, Python 3.10+
- Zoom desktop client, installed and **signed in**
- OBS Studio 28+ (ships obs-websocket 5.x) with a scene that captures the screen
  you will put the Zoom window on
- OBS WebSocket server enabled: **Tools → WebSocket Server Settings → Enable**,
  port `4455`, password optional

```powershell
python -m pip install -r requirements.txt
```

## Usage

```powershell
python main.py record `
  --url "https://us02web.zoom.us/j/81234567890?pwd=abcDEF123" `
  --start "2026-11-09 18:30" `
  --output-dir "./recordings"
```

Start with a password-protected OBS server:

```powershell
$env:OBS_PASSWORD = "secret"
python main.py record --url "https://zoom.us/j/81234567890" --start "2026-11-09 18:30"
```

Validate everything without recording (or waiting):

```powershell
python main.py record --url "https://zoom.us/j/81234567890" --start "2026-11-09 18:30" --dry-run
```

| Option | Default | Meaning |
|---|---|---|
| `--url` | required | Zoom join link **or a whole pasted invitation** — the link, the spaced meeting ID and `Passcode:` are extracted, and the passcode is put in the URI so Zoom never asks for it |
| `--start` | required | Local wall-clock time, `YYYY-MM-DD HH:MM` |
| `--output-dir` | `./recordings` | Directory for the recording; OBS is pointed here when possible |
| `--obs-port` | `4455` | OBS WebSocket port |
| `--obs-password` | `$OBS_PASSWORD` | OBS WebSocket password |
| `--obs-exe` | auto-detect | `obs64.exe` to launch when OBS is not running |
| `--launch-mode` | `zoommtg` | `zoommtg` = desktop client via protocol handler, `https` = browser |
| `--join-timeout` | `300` | Seconds to wait for the meeting window |
| `--join-delay` | `0` | Extra seconds between maximising Zoom and recording |
| `--max-duration` | `0` (no limit) | Optional safety cap in seconds. By default a recording ends **only when the meeting does**. |
| `--close-grace` | `5` | Seconds without a Zoom meeting window before OBS is stopped |
| `--waiting-room-timeout` | `1800` (30 min) | Stop if still in the waiting room after this long; `0` waits forever. In-meeting recordings are unaffected |
| `--notify` | settings | Webhook POSTed on `done`/`failed`/`needs_attention`/`missed` (Slack, Discord, any JSON endpoint). Carries `text` and `content` plus machine fields; one retry, 5 s, never blocks a run. Defaults to the URL saved in the UI |
| `--steal-meeting/--no-steal-meeting` | on | Answer Zoom's "Join new meeting? … Leave & join" so the scheduled meeting starts. `--no-steal-meeting` leaves the dialog alone (the run then raises `needs_attention`) instead of pulling you out of a meeting |
| `--quality` | `auto` | `720p` / `1080p` sets OBS's canvas **and** output resolution before recording (verified by reading it back); `auto` keeps OBS's configuration |
| `--retention-days` | `0` (keep) | Delete recordings older than N days after a run — media files only, never the log or the dialog screenshots |
| `--min-free-gb` | `2` | Refuse to start below this free space; `0` disables |
| `--window-class` | none | Extra Zoom window class treated as the meeting window (repeatable) |
| `--window-title` | none | Regex on the meeting window title (case-insensitive) |
| `--dry-run` | off | Print prerequisites, connect to OBS, exit |
| `--keep-recording-on-interrupt` | off | Ctrl+C stops waiting but leaves OBS recording |
| `--fail-if-past` | off | Error instead of starting immediately when `--start` has passed |
| `--log-file` | none | Also write logs to a file |
| `-v/--verbose` | off | Debug logging (window candidates, OBS requests) |

## Add events and record them

Adding an event never starts anything by itself — a runner must be alive.

```powershell
python main.py add "11/9/26 10am https://us06web.zoom.us/j/85091120436" --note standup
python main.py add --url "https://zoom.us/j/81234567890?pwd=abc" `
  --start "11/9/26 2:00 pm" --repeat weekly
python main.py list                  # pending entries
python main.py list --all --json     # full history, machine-readable
python main.py remove 4bfaf89e       # by id
python main.py run                   # resident: wait, record, repeat
python main.py run --once            # record at most one due event, then exit
python main.py run --drain           # process what is due, exit instead of waiting
```

Dates accept `2026-11-09 10:00`, `2026-11-09T10:00`, `11/9/26 10am`,
`11/9/26 10:00` and `11/9 14:00` (current year). `--repeat weekly|weekdays`
re-arms the event after each recording (weekdays skips Saturday/Sunday).

`schedule.json` is plain JSON next to the tool (`--schedule` overrides it):

```json
{
  "version": 1,
  "entries": [
    {
      "id": "4bfaf89e",
      "url": "https://us06web.zoom.us/j/85091120436",
      "start": "2026-11-09 10:00",
      "output_dir": null,
      "note": "standup",
      "repeat": "weekly",
      "status": "pending",
      "created": "2026-11-09 09:00:00",
      "last_exit_code": null,
      "last_path": null
    }
  ]
}
```

Runner semantics:

- Picks the earliest `pending` entry, records it, writes `done`/`failed`, the
  exit code and the verified file path, then continues. It stays resident while
  the schedule is empty (Ctrl+C or `--drain` ends it) and re-reads the file
  every `--poll` seconds, so events added later are picked up.
- An entry that became due while the runner was down is recorded if it is at
  most `--max-late` seconds late (default 600); older entries become `missed`.
- Stopping: `python main.py stop` (or the GUI's **Stop runner**) writes `schedule.json.stop`, which the runner polls
  while waiting, between entries and during a recording — then it stops OBS cleanly and exits. **Never kill the
  process**: a killed runner skips `StopRecord` (the file is left open) and its entry stays `running`.
  `stop --wait 15` blocks until the runner is gone; `status` shows the pid if you must.
- Entries are strictly sequential; a meeting that starts during another is
  queued, not dropped. A lock file (`schedule.json.lock`) prevents two runners — a second one exits with
  "another runner holds …".
- A crash leaves an entry `running`; the next start marks it `failed` and warns
  about any OBS recording no entry owns (`--stop-orphan-recording` stops it).
- The recorded file is verified (non-empty, found on disk even if OBS did not
  report a path). Missing file → exit 7. Less than `--min-free-gb` free → exit 8.
- The desktop session must be unlocked. A locked session is refused up front (exit 9) instead of producing a black
  rectangle with audio, and the display is held awake for the length of the session so a screen-blank timeout
  cannot black out a meeting.
- `--output-dir` on `add` overrides the runner's default folder per meeting.

## Local calendar UI

```powershell
python main.py gui                 # http://127.0.0.1:8765, opens the browser
python main.py gui --port 8800 --no-open
```

The page has a month calendar (days with events are badged), an add form, the entry table with status and
recorded file, Start/Stop runner controls, and a live runner-log pane.

Add form:

- **Date / Time** — clicking a calendar day fills the date.
- **Zoom link** — the whole invite can be pasted; the meeting ID and passcode are extracted.
- **Repeats** — once / daily / weekly / every 2 weeks / monthly, with weekday checkboxes and an end
  (never / on a date / after N times). This builds a real RRULE, and the line under the form previews the
  next five occurrences, computed by the same code the runner uses.
- **Timezone** — a dropdown, **defaulting to Eastern (America/New_York)**. The zone is stored on the event, so
  a 10:00 Eastern meeting stays 10:00 Eastern no matter what zone this machine reports. Preview times are shown
  in the machine's zone, which is when the recording will actually start.
- **Save this one to** — per-event recordings folder, overriding the global one.
- **Zoom email** — per-event prejoin address, overriding the Settings default.
- **Settings** card — recordings folder (`settings.output_dir`), Zoom email (`settings.email`), and an optional
  **notify webhook** (`settings.notify_url`). The runner re-reads the folder per event and the webhook per run, so
  changes take effect without a restart.

Security and tokens: it binds to `127.0.0.1` only, embeds a per-launch token and rejects requests without it, a
non-local `Origin`/`Host`, or a non-JSON POST — a desktop control panel, not a network service. Only one instance
can own the port; a second launch exits with a message telling you to reuse the running one. An open tab from a
previous launch reloads itself once when it sees a 401, because its baked-in token no longer matches.

## Start at logon

```powershell
python main.py install              # Task Scheduler ONLOGON, starts it immediately
python main.py status               # runner pid, counts, next event, OBS reachability
python main.py status --json
python main.py uninstall
```

## Import a calendar (file or feed)

```powershell
python main.py import-ics calendar.ics --dry-run          # preview
python main.py import-ics calendar.ics
python main.py import-ics "https://calendar.google.com/calendar/ical/…/basic.ics"
```

Only events containing a Zoom link are imported. The RRULE is kept (`FREQ`, `INTERVAL`, `BYDAY`, `UNTIL`, `COUNT`)
and so is `TZID`, so "every 2 weeks on Wednesday at 10:00 New York" is scheduled as exactly that; a rule this
recorder cannot express (`BYMONTHDAY`, `WKST`, …) imports as a single occurrence and is logged. Duplicates are
detected by **meeting identity + start**, not by URL string, so the same meeting pasted as a link and as
"Meeting ID: …" is one entry.

**Google Calendar without OAuth:** Settings → *Integrate calendar* → copy the **secret address in iCal format**,
paste it into the Settings card's **Calendar feed** field (or pass `--calendar-url`) and the runner re-imports it
every 15 minutes. Deletions in Google are not propagated — a cancelled meeting stays scheduled and the join simply
fails — and Google refreshes the feed with some delay.

## What happens, in order

1. `--start` is parsed as naive local time. If it is in the future, a live
   countdown blocks in 0.5 s slices; remaining time is recomputed from the wall
   clock, so the deadline does not drift and Ctrl+C works instantly.
2. The link is converted to `zoommtg://zoom.us/join?action=join&confno=…&pwd=…`
   and handed to the OS (`os.startfile`), which launches the Zoom client. If the
   protocol handler is missing, the raw link opens in the browser instead.
3. Top-level windows are enumerated, filtered to `Zoom.exe`, scored by window
   class/title, and the meeting window is restored + maximised
   (`ShowWindow(SW_RESTORE)` then `SW_MAXIMIZE`), then raised to the foreground
   best-effort.
4. OBS is connected over WebSocket, `GetVersion` confirms the session, and the
   record directory is redirected to `--output-dir`.
5. `StartRecord` is sent and **confirmed** by polling `GetRecordStatus` — the
   tool never assumes the recording started.
6. The **scene is configured** for the meeting window and Zoom's audio (globals muted), and once per second the
   meeting **window handle**, the Zoom process, the OBS record state, free disk space and the capture itself are
   checked. Ten seconds in, and then every minute, a frame is read back from OBS: if it is black (minimised window,
   locked session) the capture is re-pointed once and `needs_attention` is raised — instead of silently recording
   black for an hour.
7. On exit, `StopRecord` is sent and confirmed, the original OBS recording
   directory is restored, the socket is closed, and the file path reported by
   OBS (`RecordStateChanged`) is logged.

### Why the window handle and not the process

`Zoom.exe` keeps running after a meeting ends (tray icon, home window), so
"process exited" would never fire and "process alive" is always true. The stop
condition is therefore the disappearance of the meeting window that was
maximised, with process exit as a secondary trigger. There is **no time limit by
default**: a meeting that runs over keeps recording until the host closes it (or you are removed from it).
`--max-duration` is available as an opt-in cap.

## OBS fallback when OBS is not running

If the WebSocket is unreachable **and** no `obs*.exe` process exists, the tool
launches `obs64.exe --startrecording --minimize-to-tray` with the executable's
own directory as the working directory (OBS requires it), then waits up to 45 s
for the WebSocket to come up so it can still stop the recording.

Consequences to know about:

- The first file is opened by OBS's *configured* recording folder, so
  `--output-dir` cannot be applied to that recording. The tool warns about this.
- `obs --stoprecording` **does not exist**; without a reachable WebSocket there
  is no way to stop the recording from this tool. You are told to stop it
  manually.
- If OBS is already running but its WebSocket server is disabled, the tool does
  not launch a second instance (OBS does not support that usefully). It exits
  with code 3 and the exact setting to enable.

`SetRecordDirectory` needs obs-websocket ≥ 5.3. On older builds the tool falls
back to the `SimpleOutput.FilePath` (or `AdvOut.RecFilePath`) profile parameter
and restores the previous value on exit.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | recorded and stopped cleanly (or cancelled before recording) |
| 1 | bad input, Zoom could not be launched, or OBS was already recording |
| 2 | invalid CLI arguments (Click) |
| 3 | OBS unreachable / authentication failed / OBS not found |
| 4 | no Zoom meeting window within `--join-timeout` |
| 5 | `StartRecord` was rejected or never confirmed |
| 6 | WebSocket lost during recording (check OBS; the file may still exist) |
| 7 | no recording file was found or it is empty (check OBS/log) |
| 8 | not enough free disk space (`--min-free-gb`) |
| 9 | the Windows session is locked — the capture would be black; the entry stays pending |

## Tests

Pure-logic tests only — no Zoom, no OBS, no Windows API calls:

```powershell
python -m unittest discover -s tests -t . -v
```

Covered: date parsing (`2026-11-09 10:00`, `11/9/26 10am`, `11/9 14:00`),
countdown arithmetic/deadline drift, Zoom link → `zoommtg://` conversion,
meeting-window selection (home rejected, video pop-out outranked, class/title
overrides), OBS request confirmation/rejection, record-directory override +
restore, stop-path capture, schedule persistence/atomic writes,
due/wait/missed selection, recurrence roll-forward, runner locking, stop
markers, `.ics` parsing, disk preflight, UI token/Origin/Content-Type guards.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `OBS WebSocket not reachable at 127.0.0.1:4455` | server disabled or wrong port | enable Tools → WebSocket Server Settings; pass `--obs-port` |
| connect fails with authentication error | password mismatch | `--obs-password` or `$env:OBS_PASSWORD` |
| `Your request type is not valid` in OBS logs | `obs-websocket-py` 0.5.x installed | `pip install "obs-websocket-py==1.0"` |
| `no Zoom meeting window after 300s` | link opened in browser, waiting room, or a Zoom build with different window classes | confirm the desktop client is signed in; join manually; re-run with `-v` to see candidate windows; raise `--join-timeout` |
| recording stops after a second | OBS output rejected `StartRecord` (disk full, output busy) | check OBS log; free space; close other recordings |
| recording lands in the wrong folder | older OBS without `SetRecordDirectory` and unreadable profile key | set the folder in OBS, or use Simple/Advanced output with a file path |
| Zoom window maximises but is not in front | Windows foreground lock | the maximised window is still recorded; click it once if needed |
| recording keeps running after Ctrl+C | `--keep-recording-on-interrupt`, or the WebSocket died | stop it in OBS |

## Manual acceptance checklist

Run these once on the target machine; they cannot be automated here.

1. `--dry-run` reports `OBS check failed` while OBS is closed, and the OBS
   version + `obs recording: False` while OBS is running with the socket on.
2. Schedule a test meeting ~2 minutes ahead. Countdown ticks down and hits the
   start time exactly.
3. Zoom opens, the meeting window is maximised on screen.
4. OBS switches to recording within ~1 s of the maximise.
5. The recording appears in `--output-dir` (or the documented fallback folder).
6. Close the meeting window → recording stops, the saved path is logged, and the
   file plays.
7. Start a recording manually in OBS, run the tool → it exits 1 without
   touching the existing recording.
8. Kill OBS mid-recording → the tool exits 6 and says the recording may still
   exist.
9. Ctrl+C during recording stops it; with `--keep-recording-on-interrupt` it does
   not.

## Limitations

- Windows only; no macOS/Linux support.
- **OBS is configured for you**: the scene, the Zoom-window capture (DXGI — Windows Graphics Capture renders
  Zoom's CEF windows black) and per-process audio. OBS's global `Desktop Audio`/`Mic/Aux` devices are muted on
  every run, because the desktop device already contains Zoom and would record every voice twice (echo).
- Your microphone is **not** recorded. There is no per-event mic setting yet.
- Waiting rooms are not admitted automatically — nothing on the client side can. Recording starts on the waiting
  room window and keeps going; if you are admitted, the capture re-points to the meeting window automatically. If
  the host never admits you, the recording stops once `--waiting-room-timeout` (30 min by default) is exceeded, and
  the waiting-room window is left open — closing it raises Zoom's "End meeting or leave meeting?" dialog, and a
  blind click there can end the host's meeting. The next scheduled run dismisses it with the *Leave & join* rule.
- Zoom's "Enter your email and name" prompt is answered from: the event's own **Zoom email** → the **Zoom email**
  in the Settings card (stored as `settings.email` in `schedule.json`) → an address Zoom spells out in its own
  config (`zoom_account.discover()`) → nothing. There is **no address in the source**; with nothing configured the
  prompt is left alone and the run reports `needs_attention`. Zoom keeps the signed-in address in encrypted
  config, so it cannot be read reliably — set it once in the UI.
- A meeting passcode is taken from the link (`?pwd=…`), from a `Passcode:` line in a pasted invite, or from
  `--passcode` — all three end up in the `zoommtg://` URL, so the client is never asked to type one. If none is
  known and the meeting requires one, Zoom's prompt is left alone and the run reports `needs_attention`.
- The "Join new meeting? … Leave & join" dialog is dismissed automatically so a scheduled meeting can start. It
  leaves whatever meeting that Zoom client is already in; pass `--no-steal-meeting` (or remove the rule from
  `rules.json`) to refuse instead.
- Window class names are version-dependent. `main.py inspect` dumps every Zoom window, its OCR words and the
  click it would make; `rules.json` is data, so a relabelled button is a config change, not a code change.
- The recording container is forced to **mkv** before every start. Measured: force-killing OBS during an
  `hybrid_mp4` recording leaves a **0-byte** file (the footer is written on shutdown), while mkv keeps everything
  written so far; `postprocess` remuxes it to mp4 afterwards.
- If OBS dies (or its socket drops) mid-meeting, the recorder relaunches OBS, keeps the finished part and resumes
  into a `…-part-2` file — up to two resumes per meeting. A part shorter than OBS's first flushed frame is
  discarded rather than left as a 0-byte file.
- `.ics` import still flattens complex RRULEs (`BYDAY=MO,WE`, `COUNT`, `UNTIL`) to `weekly`; edit the entry
  afterwards, or add it with `--rrule`.
- The GUI is a localhost control panel with a per-launch token, not a multi-user service; do not bind it to a
  public interface.
