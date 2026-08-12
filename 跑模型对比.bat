@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

python tools\bench_wizard.py
echo.
pause
exit /b 0

:nopython
echo.
echo 找不到 python。请先安装 Python 3.10 或更高版本，安装时勾选 Add to PATH。
echo.
pause
exit /b 1
