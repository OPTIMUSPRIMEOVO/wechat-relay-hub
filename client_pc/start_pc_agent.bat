@echo off
cd /d "%~dp0"

set "HUB_URL=http://100.127.152.105:8000"

echo ==============================================
echo   WeChat Relay Hub - PC Node Agent
echo ==============================================
echo.
echo [1] Mock Mode (Simulation test)
echo [2] Real WeChat Mode (wxauto automation)
echo.
set "DRIVER=mock"
set /p "MODE_INPUT=Select mode [1/2, default 1]: "
if "%MODE_INPUT%"=="2" set "DRIVER=wxauto"

echo.
echo Default URL: %HUB_URL%
set /p "USER_INPUT=Press Enter to use default, or type new URL: "
if not "%USER_INPUT%"=="" set "HUB_URL=%USER_INPUT%"

echo.
echo Starting PC Agent with driver [%DRIVER%] -> %HUB_URL% ...
echo.
python pc_agent.py --url "%HUB_URL%" --key node-static-key-9527 --driver %DRIVER% --simulate

pause
