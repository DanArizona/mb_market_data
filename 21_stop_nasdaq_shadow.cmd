@echo off
setlocal
cd /d "%~dp0"
python -I -B "%~dp0experiments\nasdaq_shadow_20261019\run_review.py" stop
exit /b %errorlevel%
