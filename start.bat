@echo off
setlocal
cd /d "%~dp0" || goto :start_failed

if not defined PORT set "PORT=8765"
echo Pelican Watch: http://127.0.0.1:%PORT%/
echo Admin: http://127.0.0.1:%PORT%/admin

if exist ".venv\Scripts\python.exe" goto :run_venv
where py >nul 2>&1
if not errorlevel 1 goto :run_py
where python >nul 2>&1
if not errorlevel 1 goto :run_python
echo Python 3 was not found. Install Python and the dependencies in README.md. 1>&2
goto :start_failed

:run_venv
".venv\Scripts\python.exe" "app\server.py"
goto :finished

:run_py
py -3 "app\server.py"
goto :finished

:run_python
python "app\server.py"

:finished
set "PELICAN_EXIT=%errorlevel%"
if not "%PELICAN_EXIT%"=="0" (
    echo.
    echo Pelican Watch exited with code %PELICAN_EXIT%.
    pause
)
exit /b %PELICAN_EXIT%

:start_failed
echo Pelican Watch could not start.
pause
exit /b 1
