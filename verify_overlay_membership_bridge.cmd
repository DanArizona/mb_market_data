@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONPATH=%~dp0src;%~dp0tests;%PYTHONPATH%"
pushd "%~dp0"
call verify_overlay_workspace.cmd
if errorlevel 1 (popd & exit /b 1)
call verify_membership_operator.cmd
if errorlevel 1 (popd & exit /b 1)
python -u -B -m unittest -v test_overlay_membership_bridge
set "MB_VERIFY_EXIT=%ERRORLEVEL%"
popd
exit /b %MB_VERIFY_EXIT%
