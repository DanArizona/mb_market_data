@echo off
setlocal EnableExtensions DisableDelayedExpansion
python -u -B "%~dp0probes\run_daily_session.py" %*
exit /b %errorlevel%
