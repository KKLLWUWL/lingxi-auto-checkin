@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYEXE=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
    if exist "%~dp0.venv\Scripts\python.exe" (
        set "PYEXE=%~dp0.venv\Scripts\python.exe"
    ) else (
        set "PYEXE=python"
    )
)
set PYTHONIOENCODING=utf-8

echo 正在注销「灵犀每日自动签到」...
"%PYEXE%" "%~dp0register_task.py" --uninstall
if errorlevel 1 (
    echo COM 方式失败，改用 schtasks...
    schtasks /delete /tn "灵犀每日自动签到" /f
)
echo.
echo 完成后可再次确认：任务计划程序里不应再有「灵犀每日自动签到」。
pause
