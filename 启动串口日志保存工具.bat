@echo off
setlocal
cd /d "%~dp0"
python serial_log_recorder.py
if errorlevel 1 (
    echo.
    echo Failed to start. Install the dependency with:
    echo     python -m pip install pyserial
    echo.
    pause
)
