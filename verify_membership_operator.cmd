@echo off
setlocal
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONPATH=%~dp0src;%~dp0tests;%PYTHONPATH%"
pushd "%~dp0"
python -B -m unittest -v test_daily_operator_ui test_daily_operator_demo
set "MB_VERIFY_EXIT=%ERRORLEVEL%"
popd
exit /b %MB_VERIFY_EXIT%
