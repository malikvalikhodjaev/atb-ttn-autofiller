@echo off
setlocal
call "%~dp0start.cmd" --check
set "TTN_CHECK_RESULT=%errorlevel%"
pause
exit /b %TTN_CHECK_RESULT%
