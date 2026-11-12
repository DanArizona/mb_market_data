@echo off
setlocal
python -B "%~dp0market_data\probes\run_daily_live_overlay.py" %*
exit /b %ERRORLEVEL%
