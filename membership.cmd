@echo off
setlocal
python -B "%~dp0probes\run_daily_membership.py" %*
exit /b %ERRORLEVEL%
