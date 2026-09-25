@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   正在抓取行情并更新持仓看板 ...
echo.
"C:\Users\leyi\.workbuddy\binaries\python\versions\3.13.12\python.exe" update.py
echo.
echo   完成后可打开 dashboard.html 查看
echo.
pause
