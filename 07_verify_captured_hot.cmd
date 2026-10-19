@echo off
setlocal
title HOT-CAPTURED-VERIFY
pushd "%~dp0"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
python "experiments\hot_captured_replay_20261019\run_review.py" verify
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
