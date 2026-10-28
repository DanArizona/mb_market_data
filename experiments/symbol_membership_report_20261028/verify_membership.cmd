@echo off
setlocal
python -B "%~dp0verify.py" %*
exit /b %errorlevel%
