@echo off
setlocal EnableExtensions DisableDelayedExpansion
python -u -B "%~dp0market_data\probes\run_daily_session.py" %*
exit /b %errorlevel%
