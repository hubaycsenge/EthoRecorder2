@echo off
rem EthoRecorder2 - one-time installation on Windows 10/11.  Double-click this file.
cd /d "%~dp0"

set PY=py -3
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>nul
if errorlevel 1 (
    set PY=python
    python -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>nul
    if errorlevel 1 (
        echo Python 3.10 or newer was not found.
        echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
        pause
        exit /b 1
    )
)

if not exist .venv (
    %PY% -m venv .venv
    if errorlevel 1 goto failed
)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 goto failed

echo.
echo Installed.  Start the program by double-clicking run_windows.bat
pause
exit /b 0

:failed
echo.
echo Installation failed - see the messages above.
pause
exit /b 1
