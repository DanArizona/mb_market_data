@echo off
setlocal EnableExtensions DisableDelayedExpansion
python -u -B "%~dp0verify_overlay_workspace.py" %*
exit /b %errorlevel%
