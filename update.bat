@echo off
title AdXray Downloader - Update
cd /d "%~dp0"
echo ===========================================
echo   AdXray Downloader - One-Click Update
echo ===========================================
echo.
powershell -ExecutionPolicy Bypass -File "%~dp0_update.ps1"
echo.
pause
