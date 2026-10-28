@echo off
setlocal
python -B "%~dp0membership_report.py" --market-data-root "%~dp0..\.." %*
exit /b %errorlevel%
