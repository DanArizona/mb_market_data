@echo off
setlocal
title COMBINED-SYNTHETIC-VERIFY
python -I -B "%~dp0experiments\combined_load_20261019\run_review.py" verify
set "review_rc=%ERRORLEVEL%"
endlocal & exit /b %review_rc%
