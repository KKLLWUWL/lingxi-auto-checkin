@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ============================================================
rem  跑一次只读验证：只导出界面结构并判读当天状态，不会真的签到
rem  输出编码统一 UTF-8，避免中文乱码
rem ============================================================

echo ============================================================
echo   灵犀每日自动签到 - 首次验证
echo ============================================================
echo.
echo 这一步会：
echo   1) 关闭正在运行的灵犀，用内置调试端口重新启动它
echo   2) 连上去导出 interface / 按钮信息到 report\
echo   3) 读出当天签到状态，但不会真的点签到
echo.
echo 说明：这里特意加 --visible 让界面露出来，好看得见、截得了图。
echo       日常自动运行是静默的（无黑框、无窗口），见 2_注册开机自启.bat
echo.
echo 注意：灵犀会被重启一次，未保存的对话草稿可能丢失。
echo.
pause

rem ---- 定位 Python ----
set "PYEXE=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
    if exist "%~dp0.venv\Scripts\python.exe" (
        set "PYEXE=%~dp0.venv\Scripts\python.exe"
    ) else (
        set "PYEXE=python"
    )
)
echo 使用解释器: %PYEXE%
echo.

set PYTHONIOENCODING=utf-8
"%PYEXE%" "%~dp0lingxi_checkin.py" --dump --dry-run --visible
set RC=%errorlevel%

echo.
echo ============================================================
echo   退出码: %RC%
echo   0      = 导出完成
echo   1      = 环境类错误，看 logs\run_*.log
echo   其他   = 见 README 的退出码表
echo ============================================================
echo.
echo 排查报告在 report\，截图在 shots\
pause
