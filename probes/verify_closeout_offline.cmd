@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM Read-only offline qualification of the experimental next-session closeout gate.
REM Invoke from an isolated clone of experiment/closeout-readiness-gate-20261103.
set "ROOT=%~dp0.."
git -C "%ROOT%" status --short
if errorlevel 1 exit /b 2
for /f "delims=" %%B in ('git -C "%ROOT%" branch --show-current') do set "BRANCH=%%B"
if /I not "%BRANCH%"=="experiment/closeout-readiness-gate-20261103" (
  echo ERROR: expected isolated closeout experiment branch.
  exit /b 2
)
set "PYTHONPATH=%ROOT%\src;%ROOT%"
echo Checking maintained U.S. equity exchange calendar dependency...
python -B -c "import exchange_calendars as xc; print('exchange_calendars', xc.__version__); c=xc.get_calendar('XNYS', start='2026-01-01', end='2028-12-31'); print('XNYS early close check', c.session_close('2026-11-27'))"
if errorlevel 1 (
  echo CLOSEOUT GATE OFFLINE TESTS: BLOCKED - exchange_calendars is not installed or failed preflight.
  echo Review the optional closeout dependency before changing the production sea-green environment.
  exit /b 2
)
echo Offline-only closeout readiness and one-password orchestration tests.
python -B -m unittest discover -s "%ROOT%\tests" -p "test_next_session_*.py" -v
if errorlevel 1 exit /b 1
python -B -m unittest discover -s "%ROOT%\tests" -p "test_daily_closeout_*.py" -v
if errorlevel 1 exit /b 1
python -B -m unittest discover -s "%ROOT%\tests" -p "test_equity_closeout_calendar.py" -v
if errorlevel 1 exit /b 1
python -B -m unittest discover -s "%ROOT%\tests" -p "test_daily_universe_snapshot.py" -v
if errorlevel 1 exit /b 1
python -B -m unittest discover -s "%ROOT%\tests" -p "test_daily_universe_workflow.py" -v
if errorlevel 1 exit /b 1
echo CLOSEOUT GATE OFFLINE TESTS: PASS
exit /b 0
