@echo off
setlocal
title COMBINED-SCHWAB-STOP
python -I -B "%~dp0experiments\combined_schwab_20261019\run_review.py" stop
set "review_rc=%ERRORLEVEL%"
endlocal & exit /b %review_rc%
