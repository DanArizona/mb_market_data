@echo off
setlocal
python -u -B "%~dp0verify_overlay.py"
exit /b %ERRORLEVEL%
