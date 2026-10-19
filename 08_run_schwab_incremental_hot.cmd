@echo off
setlocal
title HOT-INCREMENTAL-SCHWAB
pushd "%~dp0"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
python "experiments\hot_captured_replay_20261019\run_review.py" schwab
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
