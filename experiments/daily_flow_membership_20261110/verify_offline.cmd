@echo off
setlocal EnableExtensions DisableDelayedExpansion
echo Offline verification only. No real credentials, provider calls, or production writes.
python -B "%~dp0verify_offline.py"
exit /b %errorlevel%
