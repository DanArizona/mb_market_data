@echo off
setlocal
title HOT-SCALE-VERIFY
pushd "%~dp0"
if errorlevel 1 exit /b 1
python "experiments\hot_scale_trial_20261018\run_review.py" verify
set "trial_exit=%errorlevel%"
popd
exit /b %trial_exit%
