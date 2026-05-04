@echo off
title AdXray DB Notify
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" db_notify.py
    goto :end
)

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found
    pause
    exit /b 1
)
python db_notify.py

:end
pause
