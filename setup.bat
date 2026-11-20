@echo off
rem One-time setup / repair for this checkout. Idempotent: safe to run again after a pull.
rem Creates the venv if missing, installs requirements, stops any stray runner, registers the
rem logon task plus the watchdog, then prints status. No admin rights needed (per-user tasks).
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"

echo [1/4] interpreter
if exist "%PY%" (
  echo       .venv present
) else (
  echo       creating .venv with py -3.13 ...
  py -3.13 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul
  if not exist "%PY%" (
    echo ERROR: could not create .venv. Install Python 3.13 for this user ^("py -3.13 -V" must work^),
    echo        then run this script again.
    exit /b 2
  )
)

echo [2/4] dependencies
"%PY%" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo ERROR: pip install failed. Check the network, then run this script again.
  exit /b 3
)
"%PY%" -c "import ocr; assert ocr.available(), ocr.unavailable_reason()" 2>nul
if errorlevel 1 (
  echo WARNING: Windows OCR is not available - joins will stall at Zoom's email prompt.
  echo          Check that the Windows language pack for this user includes OCR.
)

echo [3/4] stopping any running recorder
"%PY%" main.py stop --wait 20 >nul 2>&1
if exist "schedule.json.lock" del /q "schedule.json.lock" >nul 2>&1

echo [4/4] registering tasks and shortcuts
"%PY%" main.py install --restart-on-failure --watchdog --shortcuts
if errorlevel 1 (
  echo.
  echo ERROR: Task Scheduler refused the registration.
  echo        Run this script from a normal PowerShell/terminal of YOUR session ^(not a service,
  echo        not an elevated-only shell^), or start the runner manually for today:
  echo            .venv\Scripts\python.exe main.py run
  exit /b 4
)

echo.
echo Done. From now on Windows starts the runner at logon, restarts it on failure,
echo and runs "run --drain" every 5 minutes when the resident runner is gone.
echo Check any time with:  doctor.bat
exit /b 0
