@echo off
setlocal
title COMBINED-SYNTHETIC-RUN
python -I -B "%~dp0experiments\combined_load_20261019\run_review.py" run
set "review_rc=%ERRORLEVEL%"
endlocal & exit /b %review_rc%
