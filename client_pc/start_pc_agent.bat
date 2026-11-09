@echo off
chcp 65001 >nul
title WeChat Relay Hub - PC 节点客户端

cd /d "%~dp0"

echo ============================================================================
echo   WeChat Relay Hub - Windows PC 微信主力节点启动脚本
echo ============================================================================
echo.

:: 默认连接的 VAIO IP 地址（如果你知道了 VAIO 的 IP，可以直接修改下面这一行，例如 http://192.168.1.50:8000）
set "DEFAULT_URL=http://127.0.0.1:8000"

set /p "TARGET_URL=请输入 VAIO 中枢地址 [直接回车使用默认 %DEFAULT_URL%]: "
if "%TARGET_URL%"=="" set "TARGET_URL=%DEFAULT_URL%"

echo.
echo [1/2] 正在检查并安装 Python 依赖库 (websockets, httpx)...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [错误] Python 依赖安装失败，请确认系统已安装 Python 并已添加至环境变量 PATH。
    pause
    exit /b 1
)

echo.
echo [2/2] 正在启动 PC 微信节点连接中枢: %TARGET_URL% ...
echo 模式: 联调模拟 (--simulate) + 默认驱动 (mock)
echo.
python pc_agent.py --url %TARGET_URL% --key node-static-key-9527 --simulate

pause
