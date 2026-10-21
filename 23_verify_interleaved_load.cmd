@echo off
setlocal
title INTERLEAVED-LOAD-VERIFY
python -I -B "%~dp0experiments\interleaved_load_20261021\interleaved_review.py" verify
set "review_rc=%ERRORLEVEL%"
endlocal & exit /b %review_rc%
