@echo off
setlocal
title HOT-PAYLOAD-EXPORT
pushd "%~dp0"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
python "experiments\hot_incremental_20261019\export_payloads.py"
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
