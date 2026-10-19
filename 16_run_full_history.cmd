@echo off
setlocal
cd /d "%~dp0"
python -I -B "%~dp0experiments\polling_history_20261019\run_review.py" full
exit /b %errorlevel%
