@echo off
setlocal
python -B "%~dp0probes\run_daily_live_overlay.py" %*
exit /b %ERRORLEVEL%
