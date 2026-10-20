@echo off
setlocal
cd /d "%~dp0"
set "TTN_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%TTN_PYTHON%" goto bundled
where py >nul 2>&1
if not errorlevel 1 goto python_launcher
where python >nul 2>&1
if not errorlevel 1 goto system_python
echo Python 3.10 or newer is required. See README.txt.
pause
exit /b 1
:bundled
"%TTN_PYTHON%" "%~dp0app.py" %*
goto finished
:python_launcher
py -3 "%~dp0app.py" %*
goto finished
:system_python
python "%~dp0app.py" %*
:finished
if errorlevel 1 if "%~1"=="" pause
endlocal
