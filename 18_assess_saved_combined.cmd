@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: call "%~f0" "FULL_PATH_TO_PREVIOUS_EXPERIMENT_RUN"
  exit /b 2
)
python -I -B "%~dp0experiments\polling_history_20261019\run_review.py" assess "%~1"
exit /b %errorlevel%
