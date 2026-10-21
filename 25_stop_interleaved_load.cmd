@echo off
setlocal
title INTERLEAVED-LOAD-STOP
python -I -B "%~dp0experiments\interleaved_load_20261021\interleaved_review.py" stop
set "review_rc=%ERRORLEVEL%"
endlocal & exit /b %review_rc%
