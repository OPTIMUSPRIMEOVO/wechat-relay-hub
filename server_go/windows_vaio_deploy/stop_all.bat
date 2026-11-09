@echo off
chcp 65001 >nul
echo 正在停止后台运行的 wechat-relay-hub.exe ...
taskkill /F /IM wechat-relay-hub.exe 2>nul
echo 已停止。
pause
