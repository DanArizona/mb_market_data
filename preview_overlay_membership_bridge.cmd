@echo off
setlocal EnableExtensions DisableDelayedExpansion
python -u -B "%~dp0probes\preview_overlay_membership_bridge.py" %*
exit /b %errorlevel%
