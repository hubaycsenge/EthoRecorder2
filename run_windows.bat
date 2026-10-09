@echo off
rem Start EthoRecorder2.  Extra options are passed on, e.g.  run_windows.bat --demo
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Not installed yet - double-click install_windows.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python -m ethorecorder2 %*
if errorlevel 1 pause
