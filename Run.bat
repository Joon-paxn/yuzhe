@echo off
chcp 65001 >nul
setlocal enableextensions
title Minecraft 自动钓鱼 - 启动器
cd /d "%~dp0"

echo ============================================
echo            Minecraft 自动钓鱼程序
echo              启动器 / 一键运行
echo ============================================
echo.

REM ===== 1. 检测 Python 运行环境 =====
echo [1/3] 检测 Python 环境...
python -c "import sys;exit(0 if sys.version_info[0]>=3 else 1)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON=python"
    echo       [OK] 已检测到 python
    goto :check_deps
)

py -3 -c "import sys;exit(0 if sys.version_info[0]>=3 else 1)" >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON=py -3"
    echo       [OK] 已检测到 py 启动器
    goto :check_deps
)

echo       [!] 未检测到 Python，尝试自动安装...
winget --version >nul 2>nul
if %errorlevel% equ 0 (
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    call :refresh_path
    python -c "import sys;exit(0 if sys.version_info[0]>=3 else 1)" >nul 2>nul
    if %errorlevel% equ 0 (
        set "PYTHON=python"
        echo       [OK] Python 安装完成
        goto :check_deps
    )
)

echo       [X] 未检测到 Python，也无法自动安装。
echo           请手动安装 Python 3: https://www.python.org/downloads/
echo           安装时请勾选 "Add Python to PATH"。
echo.
pause
exit /b 1

:check_deps
REM ===== 2. 检测依赖库（首次运行自动安装）=====
echo.
echo [2/3] 检查依赖库...
%PYTHON% -c "import cv2,numpy,mss,pyautogui,keyboard,pygetwindow,win32gui" >nul 2>nul
if %errorlevel% equ 0 (
    echo       [OK] 依赖已就绪
    goto :run
)

echo       [!] 依赖缺失，首次运行自动安装...
if not exist requirements.txt (
    echo       [X] 未找到 requirements.txt，无法安装依赖
    pause
    exit /b 1
)
%PYTHON% -m pip install --upgrade pip
%PYTHON% -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo       [X] 依赖安装失败，请检查网络后重试
    echo           或手动执行: %PYTHON% -m pip install -r requirements.txt
    pause
    exit /b 1
)
echo       [OK] 依赖安装完成

:run
REM ===== 3. 启动主程序 =====
echo.
echo [3/3] 启动 main.py
echo ============================================
echo.
%PYTHON% main.py

if %errorlevel% neq 0 (
    echo.
    echo [X] 程序异常退出，错误码: %errorlevel%
)
echo.
echo ============================================
echo 程序已结束。按任意键关闭窗口。
pause >nul
endlocal
exit /b %errorlevel%

:refresh_path
REM winget 装完 Python 后当前会话 PATH 不会自动刷新，先从注册表重读 系统+用户 PATH
set "SYS_PATH="
set "USER_PATH="
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul ^| findstr /i "REG_"') do set "SYS_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul ^| findstr /i "REG_"') do set "USER_PATH=%%B"
REM call 触发二次展开，将注册表 REG_EXPAND_SZ 中的 %%SystemRoot%% 等变量正确展开
if defined SYS_PATH if defined USER_PATH call set "PATH=%SYS_PATH%;%USER_PATH%"
REM 兜底：再手动补充常见 Python 安装路径，防止注册表读取失败
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312\;%LOCALAPPDATA%\Programs\Python\Python312\Scripts\;%PATH%"
set "PATH=%ProgramFiles%\Python312\;%ProgramFiles%\Python312\Scripts\;%PATH%"
set "PATH=%LOCALAPPDATA%\Programs\Python\Python313\;%LOCALAPPDATA%\Programs\Python\Python313\Scripts\;%PATH%"
goto :eof
