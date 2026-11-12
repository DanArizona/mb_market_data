@echo off
setlocal
python -B "%~dp0market_data\probes\run_daily_membership.py" %*
exit /b %ERRORLEVEL%
