@echo off
setlocal
title HOT-INTEGRATION-VERIFY
pushd "%~dp0"
if errorlevel 1 exit /b 1
python "experiments\hot_provider_trial_20261018\run_review.py" verify
set "trial_exit=%errorlevel%"
popd
exit /b %trial_exit%
