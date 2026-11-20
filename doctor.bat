@echo off
rem Health report for this machine: runner, OBS, OCR, tasks, last runs, newest recordings.
rem Read-only. Run it whenever you want to know whether the recorder is actually working.
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo No .venv here - run setup.bat first.
  exit /b 2
)

echo === schedule and runner ===
"%PY%" main.py status
echo.

echo === OBS ===
"%PY%" -c "import os,obs_controller as oc; c=oc.ObsController(oc.ObsConfig(password=os.environ.get('OBS_PASSWORD','').strip())); print('version  :', c.connect()); print('recording:', c.is_recording()); print('folder   :', c.record_directory()); c.close()" 2>&1 | findstr /V "obswebsocket"
echo.

echo === OCR ===
"%PY%" -c "import ocr; print('available:', ocr.available(), '|', ocr.unavailable_reason())"
echo.

echo === Windows tasks ===
schtasks /Query /TN ZoomRecorder /FO LIST 2>nul | findstr /R "TaskName Status"
schtasks /Query /TN ZoomRecorder-watch /FO LIST 2>nul | findstr /R "TaskName Status"
if errorlevel 1 echo (a task is missing - run setup.bat)
echo.

echo === last runs ===
"%PY%" main.py runs --limit 3
echo.

echo === newest files ===
"%PY%" -c "import pathlib; from store import ScheduleStore; d=pathlib.Path(ScheduleStore('schedule.json').setting('output_dir') or 'recordings'); files=sorted(d.glob('*.mp4'), key=lambda p: p.stat().st_mtime, reverse=True)[:3] if d.is_dir() else []; [print('  {:6.1f} MB  {}'.format(p.stat().st_size/1e6, p.name)) for p in files]"
exit /b 0
