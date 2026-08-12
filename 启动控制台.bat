@echo off
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 goto nopython

rem pythonw, not python: it has no console, so no black window sits behind the
rem game for the whole session. Anything that would have been printed goes to
rem 运行日志.txt instead, and a fatal error still raises a message box.
rem
rem start /b so this window closes immediately rather than waiting.
start "" /b pythonw main.py gui
exit /b 0

:nopython
echo.
echo 找不到 pythonw。请先安装 Python 3.10 或更高版本，安装时勾选 Add to PATH。
echo.
pause
exit /b 1
