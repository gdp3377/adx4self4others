@echo off
title AdXray - Sync from R2
cd /d "%~dp0"
echo ===========================================
echo   AdXray - Download Database from R2
echo ===========================================
echo.
python sync.py --force --client
echo.
pause
