@echo off
setlocal EnableExtensions DisableDelayedExpansion
echo OFFLINE synthetic quote-response diagnostics. No provider access or credential prompts.
python -u -B "%~dp0verify_quote_response_diagnostics.py"
exit /b %errorlevel%
