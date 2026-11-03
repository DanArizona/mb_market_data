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
echo Offline-only closeout readiness and one-password orchestration tests.
python -B -m unittest discover -s "%ROOT%\tests" -p "test_next_session_*.py" -v
if errorlevel 1 exit /b 1
python -B -m unittest discover -s "%ROOT%\tests" -p "test_daily_closeout_*.py" -v
if errorlevel 1 exit /b 1
echo CLOSEOUT GATE OFFLINE TESTS: PASS
exit /b 0
