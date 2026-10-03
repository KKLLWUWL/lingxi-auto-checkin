# -*- coding: utf-8 -*-
r"""
Windows 桌面通知（零第三方依赖）
================================

为什么自己写？
    pip 上的 win10toast 早已停止维护，在 Win11 上经常静默失败；
    这里直接调用系统自带的 WinRT Toast 接口，Win10 / Win11 都能用，
    失败时再降级到「托盘气泡（NotifyIcon）」，再不行就用消息框兜底。

实现要点
--------
* PowerShell 走 ``-EncodedCommand``（UTF-16LE + base64），
  中文、引号、换行都不会因为代码页而炸掉。
* 全程 ``CREATE_NO_WINDOW`` + ``STARTF_USESHOWWINDOW``，
  调用 PowerShell 时不会闪出蓝色控制台窗口。
* 通知以一个自定义的应用标识（AUMID）发出，
  首次调用时自动在 HKCU 里登记，通知来源显示为「灵犀自动签到」，
  而不是「Windows PowerShell」。

用法（独立测试）：
    python notify.py "标题" "内容"
"""

from __future__ import annotations

import base64
import subprocess
import sys
from xml.sax.saxutils import escape

# 自定义应用标识：只需要写一次注册表，系统就拿它当通知来源
APP_ID = "LingxiAutoCheckin"
APP_NAME = "灵犀自动签到"


# --------------------------------------------------------------------------
# 执行 PowerShell（无窗口）
# --------------------------------------------------------------------------
def _run_ps(script: str, wait: bool = True, timeout: float = 25.0) -> tuple[int, str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")

    startupinfo = None
    creationflags = 0
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW

    # 中文 Windows 上 PowerShell 默认输出 GBK，统一按 UTF-8 容错解码即可
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Sta", "-NonInteractive",
             "-EncodedCommand", encoded],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except FileNotFoundError:
        return 127, "找不到 powershell"

    if not wait:
        return 0, ""
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()
        return 124, "PowerShell 执行超时"
    except OSError:
        return 1, ""
    return proc.returncode, (out or b"").decode("utf-8", "replace")


def _ps_str(text: str) -> str:
    """把 Python 字符串变成 PowerShell 的单引号字面量。"""
    return "'" + text.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# 方案 A：WinRT Toast（右下角系统通知，进通知中心）
# --------------------------------------------------------------------------
def _toast_script(title: str, message: str, duration: str = "short") -> str:
    xml = (
        '<toast duration="%s">'
        '<visual><binding template="ToastGeneric">'
        "<text>%s</text><text>%s</text>"
        "</binding></visual>"
        '<audio src="ms-winsoundevent:Notification.Default"/>'
        "</toast>"
    ) % (duration, escape(title), escape(message))

    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$AUMID = " + _ps_str(APP_ID),
        "$reg = 'HKCU:\\SOFTWARE\\Classes\\AppUserModelId\\' + $AUMID",
        "if (-not (Test-Path -LiteralPath $reg)) {",
        "  New-Item -Path $reg -Force | Out-Null",
        "}",
        "New-ItemProperty -Path $reg -Name 'DisplayName' -Value "
        + _ps_str(APP_NAME)
        + " -PropertyType String -Force | Out-Null",
        "$null = [Windows.UI.Notifications.ToastNotificationManager,"
        " Windows.UI.Notifications, ContentType = WindowsRuntime]",
        "$null = [Windows.Data.Xml.Dom.XmlDocument,"
        " Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]",
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument",
        "$doc.LoadXml(" + _ps_str(xml) + ")",
        "$t = New-Object Windows.UI.Notifications.ToastNotification $doc",
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier($AUMID).Show($t)",
        "exit 0",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 方案 B：托盘气泡（老系统 / Toast 不可用时的退路）
# --------------------------------------------------------------------------
def _balloon_script(title: str, message: str) -> str:
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "Add-Type -AssemblyName System.Windows.Forms",
        "Add-Type -AssemblyName System.Drawing",
        "$n = New-Object System.Windows.Forms.NotifyIcon",
        "$n.Icon = [System.Drawing.SystemIcons]::Information",
        "$n.BalloonTipTitle = " + _ps_str(title),
        "$n.BalloonTipText = " + _ps_str(message),
        "$n.Visible = $true",
        "$n.ShowBalloonTip(8000)",
        "Start-Sleep -Seconds 7",
        "$n.Dispose()",
        "exit 0",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def notify(title: str, message: str = "", duration: str = "short") -> bool:
    """弹一条桌面通知。成功返回 True；三种方案都失败返回 False。

    不会抛异常，也不会阻塞太久（Toast 约 1 秒，降级气泡约 7 秒）。
    """
    if not message:
        message = " "
    code, out = _run_ps(_toast_script(title, message, duration), wait=True)
    if code == 0:
        return True

    # 降级：托盘气泡
    code2, out2 = _run_ps(_balloon_script(title, message), wait=True, timeout=20)
    if code2 == 0:
        return True

    sys.stderr.write(
        "[notify] 通知发送失败\n  toast: %s\n  balloon: %s\n" % (out.strip(), out2.strip())
    )
    return False


def notify_async(title: str, message: str = "") -> None:
    """发一条不等结果的通知（失败也无所谓时用）。"""
    _run_ps(_toast_script(title, message), wait=False)


if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else "灵犀自动签到 · 测试"
    m = sys.argv[2] if len(sys.argv) > 2 else "如果你看到这条，说明通知通道正常"
    ok = notify(t, m)
    print("通知已发送" if ok else "通知发送失败")
    sys.exit(0 if ok else 1)
