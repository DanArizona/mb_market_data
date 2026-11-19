@echo off
setlocal
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
python -u -B "%~dp0probes\run_daily_operator_demo.py" %*
exit /b %ERRORLEVEL%
