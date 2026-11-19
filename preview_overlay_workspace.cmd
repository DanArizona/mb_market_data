@echo off
setlocal EnableExtensions DisableDelayedExpansion
python -u -B "%~dp0probes\preview_overlay_workspace.py" %*
exit /b %errorlevel%
