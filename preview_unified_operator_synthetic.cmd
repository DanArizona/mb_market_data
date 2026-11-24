@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONPATH=%~dp0src;%~dp0tests;%PYTHONPATH%"
pushd "%~dp0"
python -u -B "probes\preview_unified_operator_synthetic.py" %*
set "MB_DEMO_EXIT=%ERRORLEVEL%"
popd
exit /b %MB_DEMO_EXIT%
