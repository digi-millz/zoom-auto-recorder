# TODO

Remaining work. Everything else — scheduling, joining, dialog handling, recording, crash resume, notifications,
recurrence/tz, calendar sync, run history, HTML reports, disk/lock/black guards — is implemented and covered by
`python -m unittest discover -s tests -t .` (286 tests). These items are open because they need something this
machine cannot provide: a live meeting, your unlocked session, or Task Scheduler rights.

## Blocked on a live session

- [ ] **Register the task** — the XML itself is now validated: Task Scheduler's own COM parser accepted it and
      read back `RestartInterval=PT1M`, `RestartCount=3`, `ExecutionTimeLimit=PT0S`,
      `MultipleInstances=2 (IgnoreNew)`, `LogonType=3 (InteractiveToken)`, `RunLevel=0`, and the action
      (`pythonw.exe main.py run … --drain`, working dir = repo). What is untested is *registration*: `schtasks
      /Create` is denied in the agent shell for any task. Run `python main.py install --restart-on-failure` from
      a normal terminal, then `schtasks /Query /TN ZoomRecorder /XML`.
- [ ] **P4: 5-minute recording acceptance** — record a real meeting, then `python main.py verify "<file>"
      --expected-minutes 5`: it checks one video stream (Zoom window only), audio present, and duration. The
      *picture* half still needs eyes: no desktop, no notifications; audio must be Zoom only (no system sounds,
      no mic) — ears.
- [ ] **P4: OBS killed while recording** — *file half verified 2026-11-14*: a real OBS 33.0.1 recording force-killed
      mid-flight leaves a playable mkv (`ffprobe`: h264 1920×1080, aac, 5.733 s, 4.3 MB). Still needs a meeting for
      the other half: the next start must resume as part 2.
- [ ] **P6: two unattended cycles** — schedule two meetings end to end with no interaction; both must be `done`
      with a verified file and a run-history line.
- [ ] **P6: kill the app mid-meeting** — *reconciliation verified 2026-11-14 with a synthetic crash*: an entry left
      `running` + a real OBS recording no entry owns → `run --drain` logs `marked 1 interrupted entry/entries as
      failed` and the orphan warning; `--stop-orphan-recording` stops it, and the file plays (`ffprobe`: 7.233 s,
      h264). Still needs a meeting to kill the runner *during* a recording.
- [ ] **P6: locked session** — lock Windows before a scheduled start; expect exit 9, a clear log line, and no
      file. (The check and the display-awake call are unit-tested; the locked state itself is not reproducible
      in CI.)

## Optional

- [ ] **Zoom-native recorder mode** — `"recorder": "obs"|"zoom"`; drives the Zoom client's own recorder
      (`Alt+R`, host/co-host only), best fidelity, no OBS scene.
- [x] **Teach mode in the UI** — a card lists one screenshot per unrecognised window class; naming the button
      appends a validated rule to `rules.json` (denylist wins, no duplicates, nothing clicked without a rule).
- [ ] **Consent notice** — post "this meeting is being recorded" in the Zoom chat when recording starts.
- [x] **Per-event microphone** — `"mic": true` per entry (UI checkbox, `add --mic`, `edit --mic`); the scene gets a
      `wasapi_input_capture` source, enabled/disabled per entry, global `Mic/Aux` stays muted.

## Done since the plan

- [x] Run history (`logs/runs.jsonl`) + `python main.py runs`
- [x] Per-run HTML report with embedded screenshots (`logs/reports/`)
- [x] `install --restart-on-failure` (Task XML) and `install --drain`
- [x] Locked-session abort (exit 9), display held awake, un-minimise on black frames
- [x] Free-text IANA timezone in the UI (any zone, not a fixed list)
- [x] `main.py edit <id>`, per-entry folder validation at save
- [x] Teach mode card in the UI (`teach.py`, `/api/shots`, `/api/rules`)
- [x] Per-event microphone (`scene.ensure_microphone`, verified against OBS 33.0.1: created enabled, disabled
      without deletion)

## Verification notes

- Full suite: `.venv\Scripts\python.exe -m unittest discover -s tests -t .`
- Static check: `.venv\Scripts\python.exe -m pyflakes *.py tests/*.py`
- Live-meeting evidence collected so far: `docs/spikes.md`
- Acceptance criteria: `PLAN.md` §5 (P4–P6)
