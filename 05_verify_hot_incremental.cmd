@echo off
setlocal
title HOT-INCREMENTAL-VERIFY
pushd "%~dp0"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
python "experiments\hot_incremental_20261019\run_review.py"
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
