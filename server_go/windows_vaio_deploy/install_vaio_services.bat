@echo off
chcp 65001 >nul
title WeChat Relay Hub - 老旧 VAIO Windows 原生安装脚本 (免 VMware / 免 Docker)

echo ============================================================================
echo   WeChat Relay Hub - VAIO Windows 原生轻量化开机自启部署脚本
echo   内存占用: wechat-relay-hub.exe (~10MB) + cloudflared.exe (~18MB)
echo ============================================================================
echo.

cd /d "%~dp0\.."
set "APP_DIR=%CD%"
set "EXE_PATH=%APP_DIR%\wechat-relay-hub.exe"
set "CFD_PATH=%APP_DIR%\cloudflared.exe"
set "VBS_PATH=%APP_DIR%\windows_vaio_deploy\start_background_hidden.vbs"

if not exist "%EXE_PATH%" (
    echo [提示] 未检测到 "%EXE_PATH%"，请先将编译好的 wechat-relay-hub.exe 放在 "%APP_DIR%" 目录下！
    pause
    exit /b 1
)

echo [1/2] 正在注册 WeChatRelayHub 开机静默自启计划任务...
schtasks /create /tn "WeChatRelayHub" /tr "wscript.exe \"%VBS_PATH%\"" /sc onlogon /rl highest /f
if %errorlevel% equ 0 (
    echo [成功] 已注册开机自动静默启动任务: WeChatRelayHub
) else (
    echo [警告] 请右键以「管理员身份」运行本脚本以注册开机任务。
)

echo.
echo [2/2] 如果你已将 cloudflared.exe 放入同目录，可通过以下命令安装 Cloudflare Tunnel 系统服务:
echo       "%CFD_PATH%" service install 你的_TUNNEL_TOKEN
echo.
echo 正在立即为你后台静默启动 wechat-relay-hub.exe ...
wscript.exe "%VBS_PATH%"
echo [完成] 服务已在后台运行！可打开任务管理器查看 wechat-relay-hub.exe (内存 < 15MB)。
pause
