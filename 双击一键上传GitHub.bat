@echo off
cd /d "%~dp0"
echo ========================================
echo  WeChat Relay Hub - Push to GitHub
echo ========================================
echo.
git push -u origin main
echo.
pause
