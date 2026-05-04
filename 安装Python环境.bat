@echo off
title AdXray Downloader - Setup
cd /d "%~dp0"

echo ===========================================
echo   AdXray Downloader - Environment Setup
echo ===========================================
echo.

:: ---- Clean broken .venv copied from another machine ----
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" --version >nul 2>&1
    if errorlevel 1 (
        echo [INFO] Broken .venv detected, removing...
        rmdir /s /q .venv 2>nul
    )
)

:: ---- Check Python ----
echo ==== 1/2 Check Python ====
python --version >nul 2>&1
if %errorlevel%==0 (
    echo [OK] Python found:
    python --version
    echo.
    echo No extra packages needed. Run "start_downloader.bat" to launch.
    echo.
    pause
    exit /b 0
)

:: ---- Auto install Python ----
echo [INFO] Python not found, downloading...
echo.

echo ==== 2/2 Install Python ====
set "PY_URL=https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
set "PY_EXE=%TEMP%\python-installer.exe"

echo Download: %PY_URL%
powershell -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_EXE%' -UseBasicParsing }"

if not exist "%PY_EXE%" (
    echo [ERROR] Download failed! Please install Python manually:
    echo   https://www.python.org/downloads/
    echo   Check "Add Python to PATH" and "tcl/tk" during install
    echo.
    pause
    exit /b 1
)

echo [INFO] Installing (auto add PATH + tcl/tk)...
"%PY_EXE%" /passive InstallAllUsers=0 PrependPath=1 Include_tcltk=1 Include_pip=1

if %errorlevel% neq 0 (
    echo [ERROR] Install may have failed, please run manually: %PY_EXE%
    echo.
    pause
    exit /b 1
)

echo.
echo ===========================================
echo   Setup complete!
echo   Close this window, then run
echo   "start_downloader.bat" to launch
echo ===========================================
echo.
pause
