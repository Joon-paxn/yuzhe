@echo off
setlocal enableextensions
title Minecraft AutoFishing - Launcher
cd /d "%~dp0"

REM Keep launcher output readable on Windows regardless of the user's active code page.
chcp 65001 >nul
set "PYTHONUTF8=1"

echo ============================================
echo         Minecraft AutoFishing
echo           Launcher / One-click Run
echo ============================================
echo.

REM ===== 1. Detect Python =====
echo [1/3] Detecting Python...
python -c "import sys;exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON=python"
    echo       [OK] python found
    goto :check_deps
)

py -3 -c "import sys;exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON=py -3"
    echo       [OK] py launcher found
    goto :check_deps
)

echo       [!] Python not found, trying auto-install...
winget --version >nul 2>nul
if %errorlevel% equ 0 (
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    call :refresh_path
    python -c "import sys;exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if %errorlevel% equ 0 (
        set "PYTHON=python"
        echo       [OK] Python installed
        goto :check_deps
    )
)

echo       [X] Python not found and auto-install failed.
echo           Please install Python 3 manually: https://www.python.org/downloads/
echo           Make sure to check "Add Python to PATH".
echo.
pause
exit /b 1

:check_deps
REM ===== 2. Check dependencies (auto-install on first run) =====
echo.
echo [2/3] Checking dependencies...
%PYTHON% -c "import cv2,numpy,mss,PIL,pyautogui,keyboard,pygetwindow,win32gui,rapidocr,onnxruntime" >nul 2>nul
if %errorlevel% equ 0 (
    echo       [OK] Dependencies ready
    goto :run
)

echo       [!] Missing dependencies, installing...
if not exist requirements.txt (
    echo       [X] requirements.txt not found
    pause
    exit /b 1
)
%PYTHON% -m pip install --upgrade pip
%PYTHON% -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo       [X] Dependency install failed, check network
    echo           Or run manually: %PYTHON% -m pip install -r requirements.txt
    pause
    exit /b 1
)
echo       [OK] Dependencies installed

:run
REM ===== 3. Launch main program =====
echo.
echo [3/3] Launching main.py
echo ============================================
echo.
%PYTHON% -X utf8 main.py

if %errorlevel% neq 0 (
    echo.
    echo [X] Program exited with error code: %errorlevel%
)
echo.
echo ============================================
echo Program finished. Press any key to close.
pause >nul
endlocal
exit /b %errorlevel%

:refresh_path
REM Re-read PATH from registry after winget installs Python
set "SYS_PATH="
set "USER_PATH="
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul ^| findstr /i "REG_"') do set "SYS_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul ^| findstr /i "REG_"') do set "USER_PATH=%%B"
if defined SYS_PATH if defined USER_PATH call set "PATH=%SYS_PATH%;%USER_PATH%"
REM Fallback: add common Python install paths
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312\;%LOCALAPPDATA%\Programs\Python\Python312\Scripts\;%PATH%"
set "PATH=%ProgramFiles%\Python312\;%ProgramFiles%\Python312\Scripts\;%PATH%"
set "PATH=%LOCALAPPDATA%\Programs\Python\Python313\;%LOCALAPPDATA%\Programs\Python\Python313\Scripts\;%PATH%"
goto :eof
