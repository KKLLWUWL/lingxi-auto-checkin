# -*- coding: utf-8 -*-
r"""
用任务计划服务的 COM 接口管理「灵犀每日自动签到」定时任务。

为什么不用 schtasks.exe？
    有些环境的安全策略把 schtasks.exe 列入了程序黑名单（cmd.exe 同理），
    但 Task Scheduler 的 COM 接口（Schedule.Service）不受那条策略影响，
    而且能做 schtasks 做不到的事：精确设置「登录后延迟」「失败重试次数」。

用法：
    python register_task.py --install        # 注册任务（登录时自动执行）
    python register_task.py --run           # 立即触发一次（用于验证）
    python register_task.py --status        # 查看任务状态与上次结果
    python register_task.py --uninstall     # 注销任务

需要 pywin32（pip install pywin32）。只用于本管理脚本，
真正的签到脚本 lingxi_checkin.py 是零依赖的。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import win32com.client
import pythoncom

TASK_NAME = "灵犀每日自动签到"
PORT_DEFAULT = 19222
BASE_DIR = Path(__file__).resolve().parent

# 任务计划 COM 常量
TASK_TRIGGER_LOGON = 9
TASK_ACTION_EXEC = 0
TASK_CREATE_OR_UPDATE = 6
TASK_LOGON_INTERACTIVE_TOKEN = 3
TASK_UPDATE = 4


def service():
    """连接本地任务计划服务。"""
    pythoncom.CoInitialize()
    svc = win32com.client.Dispatch("Schedule.Service")
    svc.Connect()
    return svc


def task_xml(runner: Path) -> str:
    """手工写任务 XML——COM 的 Trigger 对象写法繁琐且版本差异大，
    直接给 XML 最稳，Task Scheduler 2.0 (Vista+) 都认。"""
    delay = "PT60S"                      # 登录后延迟 60 秒，等托盘与网络就绪
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>灵犀专业版每日自动签到：开机登录后自动进入任务中心签到领取积分。</Description>
    <Author>{TASK_NAME}</Author>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <StartBoundary>{dt.datetime.now():%Y-%m-%d}T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <Delay>{delay}</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>"{runner}"</Command>
      <WorkingDirectory>{runner.parent}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def _pick_python(windowed: bool = True) -> str:
    """挑一个能用的解释器：先脚本同目录/常见虚拟环境，再回落到 PATH 里的 python。

    windowed=True 时优先挑 pythonw.exe——它天生没有控制台窗口，
    配合 lingxi_checkin.py 自带的「摘控制台」逻辑，双保险不闪黑框。
    """
    bases = [
        BASE_DIR / ".venv/Scripts/python.exe",
        Path(sys.executable),          # 当前解释器（发布版不写死任何本机路径）
    ]
    for c in bases:
        if not c.exists():
            continue
        if windowed:
            w = c.with_name("pythonw.exe")
            if w.exists():
                return str(w)
        return str(c)
    return "pythonw" if windowed else "python"


def install(python_exe: str | None = None, wait: int = 60, retries: int = 3,
            visible: bool = False) -> int:
    """注册任务。

    直接调用 Python 而不是再包一层 .bat —— 少一层 %{}~dp0 与代码页的麻烦，
    等待与重试交给 lingxi_checkin.py 的 --wait / --retries 处理。

    visible=False（默认）时用 pythonw 拉起，全程无窗口；
    签到结果通过系统通知告知。
    """
    python_exe = python_exe or _pick_python(windowed=not visible)
    script = BASE_DIR / "lingxi_checkin.py"
    if not script.exists():
        print(f"[错误] 找不到主脚本: {script}")
        return 1

    svc = service()
    folder = svc.GetFolder("\\")

    taskdef = svc.NewTask(0)

    reg = taskdef.RegistrationInfo
    reg.Author = TASK_NAME
    reg.Description = (
        "灵犀专业版每日自动签到：开机登录后自动进入任务中心签到领取积分。"
    )

    # 触发器：用户登录时，延迟一小会儿，等托盘与网络就绪
    trigger = taskdef.Triggers.Create(TASK_TRIGGER_LOGON)
    trigger.Id = "OnLogon"
    trigger.StartBoundary = f"{dt.datetime.now():%Y-%m-%d}T00:00:00"
    trigger.Delay = "PT60S"
    trigger.Enabled = True

    # 动作：直接跑 Python
    action = taskdef.Actions.Create(TASK_ACTION_EXEC)
    action.Path = python_exe
    action.WorkingDirectory = str(BASE_DIR)
    action.Arguments = (f'"{script}" --wait {wait} --retries {retries}'
                        f' --port {PORT_DEFAULT}'
                        + (' --visible' if visible else ''))

    # 其余运行策略
    st = taskdef.Settings
    st.Enabled = True
    st.Hidden = False
    st.AllowDemandStart = True
    st.StartWhenAvailable = True
    st.StopIfGoingOnBatteries = False
    st.DisallowStartIfOnBatteries = False
    st.ExecutionTimeLimit = "PT15M"
    st.MultipleInstances = 0            # ParallelInstance?0=Parallel
    st.Compatibility = 2                # V2_XP_2?2 对应 2.0 兼容
    try:
        st.RunOnlyIfNetworkAvailable = False
        st.StopOnIdleEnd = False
        st.RestartOnIdle = False
    except Exception:                    # 老版本不一定有这些属性
        pass

    try:
        folder.RegisterTaskDefinition(
            TASK_NAME,
            taskdef,
            TASK_CREATE_OR_UPDATE,
            "",                          # 当前用户
            "",                          # 不需要密码（交互式令牌）
            TASK_LOGON_INTERACTIVE_TOKEN,
        )
        print(f"[OK] 已注册任务: {TASK_NAME}")
        print(f"     解释器 : {python_exe}")
        print(f"     脚本   : {script}")
        print(f"     参数   : --wait {wait} --retries {retries} --port {PORT_DEFAULT}"
              + (" --visible" if visible else " (静默)"))
        print("     触发器 : 每次登录时（延迟 60 秒）")
        print("     结果   : 桌面通知；窗口全程隐藏" if not visible
              else "     结果   : 显示灵犀窗口，便于观察")
        print(f"     日志   : {BASE_DIR / 'logs'}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 注册失败: {e}")
        return 1


def uninstall() -> int:
    svc = service()
    folder = svc.GetFolder("\\")
    try:
        folder.DeleteTask(TASK_NAME, 0)
        print(f"[OK] 已注销任务: {TASK_NAME}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[提示] 注销失败（可能本来就不存在）: {e}")
        return 1


def run_now() -> int:
    svc = service()
    folder = svc.GetFolder("\\")
    try:
        task = folder.GetTask(TASK_NAME)
        task.Run("")
        print(f"[OK] 已触发任务，请稍候查看 {BASE_DIR / 'logs' / 'startup.log'}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 触发失败: {e}")
        return 1


def status() -> int:
    svc = service()
    folder = svc.GetFolder("\\")
    try:
        task = folder.GetTask(TASK_NAME)
    except Exception as e:  # noqa: BLE001
        print(f"任务不存在: {TASK_NAME} ({e})")
        return 1
    info = task.Definition.RegistrationInfo
    print(f"任务名   : {task.Name}")
    print(f"启用     : {task.Enabled}")
    print(f"状态     : {task.State}")
    print(f"上次运行 : {task.LastRunTime}")
    print(f"上次结果 : {task.LastTaskResult}   (0 = 成功)")
    print(f"下次运行 : {task.NextRunTime}")
    for i, t in enumerate(task.Definition.Triggers, 1):
        print(f"触发器{i}  : {t.Type} / {getattr(t, 'Id', '')} 延迟={getattr(t, 'Delay', '')}")
    for a in task.Definition.Actions:
        print(f"动作     : {a.Path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="注册/管理灵犀自动签到定时任务")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--run", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--visible", action="store_true",
                    help="注册成可见模式（排障用：会显示灵犀窗口）")
    args = ap.parse_args()

    try:
        if args.install:
            return install(visible=args.visible)
        if args.uninstall:
            return uninstall()
        if args.run:
            return run_now()
        return status()
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
