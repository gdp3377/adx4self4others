@echo off
setlocal
title Force Refresh Data (re-download cache.db from R2)

set "ROOT=%~dp0"

set "PY="
for %%C in (
    "%ROOT%python\python.exe"
    "%ROOT%..\python\python.exe"
) do (
    if exist "%%~C" (
        set "PY=%%~C"
        goto :found
    )
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    set "PY=python"
    goto :found
)

echo [ERR] Python not found. Install Python 3.10+ or use the bundled one.
pause
exit /b 1

:found
echo ============================================================
echo  Force re-download cache.db from R2 (bypass today's cache)
echo ============================================================
echo.

set "SYNC_PY=%ROOT%..\LocalDashboard\sync.py"
if not exist "%SYNC_PY%" set "SYNC_PY=%ROOT%LocalDashboard\sync.py"
if not exist "%SYNC_PY%" (
    echo [ERR] sync.py not found: %SYNC_PY%
    pause
    exit /b 1
)

"%PY%" "%SYNC_PY%" --force --client
set EXIT=%errorlevel%

echo.
if %EXIT% equ 0 (
    echo [OK] Data refreshed. Now launch the downloader to see latest remarks.
) else (
    echo [ERR] Refresh failed, exit code %EXIT%
)

echo.
pause
