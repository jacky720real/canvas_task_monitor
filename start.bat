@echo off
chcp 65001 >nul
title Canvas Task Monitor
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [错误] 未找到虚拟环境 .venv
    echo.
    echo 请先在此目录打开 PowerShell，运行：
    echo   uv venv --python 3.12
    echo   uv pip install -e ".[dev,mcp]"
    echo.
    pause
    exit /b 1
)

echo.
echo 正在启动 Canvas Task Monitor...
echo.
".venv\Scripts\python.exe" web_main.py

echo.
echo 服务已停止。
pause

