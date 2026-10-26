@echo off
setlocal
python "%~dp0membership_report.py" --market-data-root "%~dp0..\.." %*
exit /b %errorlevel%
