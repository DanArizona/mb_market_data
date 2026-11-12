@echo off
setlocal
python -u -B "%~dp0market_data\probes\run_daily_health.py" alarm-test
exit /b %errorlevel%
