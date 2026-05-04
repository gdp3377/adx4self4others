@echo off
title AdXray Downloader
cd /d "%~dp0"

:: Prefer local .venv if available
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" --version >nul 2>&1
    if not errorlevel 1 (
        ".venv\Scripts\python.exe" downloader.py
        goto :end
    )
    echo [WARN] .venv is broken, falling back to system Python
    rmdir /s /q .venv 2>nul
)

:: Fallback to system Python
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found. Please run setup first.
    pause
    exit /b 1
)
python downloader.py

:end
if errorlevel 1 (
    echo.
    echo [ERROR] Failed. Please check Python version ^>= 3.10
    pause
)
