@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  注册开机自启：优先用任务计划 COM 接口，失败再退回到 schtasks
rem ============================================================

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

echo 方式一：通过任务计划 COM 接口注册...
rem 静默模式：用 pythonw 拉起，开机后不会闪黑框，灵犀窗口也全程隐藏，
rem 签到结果以系统通知告知。想看界面请把下面的 --install 换成 --install --visible
"%PYEXE%" "%~dp0register_task.py" --install
if not errorlevel 1 goto :ok

echo.
echo COM 方式失败（多半是缺 pywin32），改用 schtasks...
set "PYWEXE=%PYEXE:python.exe=pythonw.exe%"
if not exist "%PYWEXE%" set "PYWEXE=%PYEXE%"
schtasks /create /tn "灵犀每日自动签到" ^
  /tr "\"%PYWEXE%\" \"%~dp0lingxi_checkin.py\" --wait 60 --retries 3 --port 19222" ^
  /sc onlogon /delay 0000:01 /f
if errorlevel 1 (
    echo.
    echo [失败] 两种方式都没成功。请右键本文件选择「以管理员身份运行」。
    pause
    exit /b 1
)

:ok
echo.
echo [OK] 已注册。查看状态可以运行：
echo      "%PYEXE%" "%~dp0register_task.py" --status
echo.
pause
