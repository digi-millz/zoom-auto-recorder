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
- [ ] **P4: 5-minute recording acceptance** — record a real meeting: the mp4 must show only the Zoom window (no
      desktop, no notifications), the audio must be Zoom only (no system sounds, no mic), and the duration must
      match the wall clock.
- [ ] **P4: OBS killed mid-meeting** — kill OBS while recording; the mkv must remain playable and the next start
      must recover it (resume as part 2, or surface the orphan with `--stop-orphan-recording`).
- [ ] **P6: two unattended cycles** — schedule two meetings end to end with no interaction; both must be `done`
      with a verified file and a run-history line.
- [ ] **P6: kill the app mid-meeting** — `Stop-Process` the runner during a recording; on restart the entry must
      go `running` → `failed` and the orphaned OBS recording must be reported.
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
