@echo off
setlocal EnableExtensions DisableDelayedExpansion
echo Full synthetic core regression for experimental quote response diagnostics.
echo No provider access, password prompt, or production files.
python -u -B "%~dp0verify_quote_response_full_offline.py"
exit /b %errorlevel%
