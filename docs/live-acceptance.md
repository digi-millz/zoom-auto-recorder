# Live acceptance

The five checks that need a real meeting (or a locked desktop), with the exact commands and the output that proves
each one. Everything else is covered by `python -m unittest discover -s tests -t .` (307 tests).

Preconditions: OBS Studio running with the WebSocket server enabled (port 4455), the Zoom desktop client signed in,
and a meeting you can host or join (passcode/disclaimer allowed — the point is that the dialogs appear).

Work in a scratch folder so nothing here touches your real schedule:

```powershell
$env:ZAR = "$env:TEMP\zar-accept"
New-Item -ItemType Directory -Force $env:ZAR | Out-Null
.venv\Scripts\python.exe main.py add --schedule $env:ZAR\schedule.json `
  --url "<your zoom link>" --start "<YYYY-MM-DD HH:MM>" --note "acceptance" --mic:$false
.venv\Scripts\python.exe main.py run --schedule $env:ZAR\schedule.json --output-dir $env:ZAR\rec
```

## 1. P4 — five minutes, window only, Zoom audio only

Join the meeting when the runner does. Talk for five minutes, share your screen once, end the meeting.

```powershell
.venv\Scripts\python.exe main.py verify $env:ZAR\rec --expected-minutes 5
```

Expect `verdict : PASS` (one video stream, audio present, duration within 10 %).

Then look and listen: only the Zoom window, no desktop or notifications; voices only, no system sounds, no echo.

## 2. P4 — OBS killed mid-meeting

Same recording, but halfway through:

```powershell
taskkill /F /IM obs64.exe
```

Expect in the runner log: `OBS stopped recording on its own … resuming as part 2`, then OBS relaunched and recording
again into a second file. After the meeting: two files, both play, the second named `… (part 2)`.

## 3. P6 — two unattended cycles

Schedule two meetings an hour apart; do not touch the machine. Expect both entries `done`, two verified files, two
lines in `logs/runs.jsonl`, and `python main.py runs` showing both.

## 4. P6 — runner killed mid-recording

During a recording:

```powershell
Stop-Process -Id (Get-Content $env:ZAR\schedule.json.lock) -Force
```

Restart the runner. Expect: `marked 1 interrupted entry/entries as failed`, the entry `failed`, and the untracked OBS
recording surfaced (`--stop-orphan-recording` stops it). The mkv must play.

## 5. P6 — locked session

Lock Windows (`Win+L`) before a scheduled start, leave it locked past the start time. Expect: exit 9, one clear log
line ("the Windows session is locked"), no file, and the entry still `pending`. Unlock; the entry records within
`--max-late` (600 s) if you unlock in time.

## Record the result

Paste the five outputs into `TODO.md`, tick the items, and delete this file if it has served its purpose.
