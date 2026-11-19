@echo off
setlocal
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
python -B "%~dp0probes\run_daily_operator.py" %*
exit /b %ERRORLEVEL%
