# -*- coding: utf-8 -*-
r"""
灵犀专业版 · 每日自动签到
=========================

用途
----
每天开机后自动打开 WPS 灵犀（桌面端），进入「任务中心」，
判断当天是否已签到；未签到则点击签到领取积分。幂等：一天多次运行只签一次。

原理
----
灵犀桌面端是 Electron 应用，其启动代码里内置了：

    const port = parseInt(process.env.MUSA_CDP_PORT ?? "", 10)
    if (port > 0) app.commandLine.appendSwitch("remote-debugging-port", port)

也就是说只要在启动时给它环境变量 ``MUSA_CDP_PORT``，它自己就会打开标准的
Chrome DevTools Protocol 端口。我们通过这个正规通道去操作界面，
**不修改它的任何文件、不注入代码、不触碰登录凭据**。

几点必须知道的运行前提（写在前面，避免踩坑）：
1. Electron 有单实例锁：第二次双击会被直接驳回。所以本脚本采取
   「先退出已有实例 → 再带端口重启」的策略，环境变量才生效。
   发生在开机后刚登录时，代价可以忽略。
2. 登录状态保存在 %APPDATA%\WPS 灵犀 的用户数据里，重启不会掉登录。
3. 签到是调用界面上那个按钮完成（等价于你手动点一下），
   而不是伪造请求，符合产品规则。

默认行为（后台静默）
--------------------
启动后立刻把自己从控制台摘下来（计划任务拉起时不会闪黑框），
灵犀窗口一露面就隐藏（Electron 建窗有延迟，脚本会持续巡视到流程结束），
全程无窗口干扰；签到完成后把灵犀整个退出（不留在托盘），
再弹一条 Windows 右下角通知，随后脚本自己结束。

退出顺序是「先礼后兵」：调它自己的退出接口 → 给窗口发 WM_CLOSE
让它走正常关闭流程 → 还不退（缩托盘的典型表现）才结束进程。

调试时想看见界面，加 ``--visible``；想让灵犀继续开着，加 ``--no-quit``。

用法
----
    python lingxi_checkin.py                # 静默执行一次签到（默认）
    python lingxi_checkin.py --dry-run      # 只判断不点，看当天状态
    python lingxi_checkin.py --dump         # 排障：导出界面/接口信息到 report/
    python lingxi_checkin.py --no-restart   # 已有一个带端口的实例时复用它
    python lingxi_checkin.py --visible      # 不隐藏窗口，用于人工观察排障
    python lingxi_checkin.py --no-quit      # 签到后不退出灵犀，让它继续开着
    python lingxi_checkin.py --no-notify    # 不弹桌面通知
    python lingxi_checkin.py --test-notify  # 只发一条测试通知就退出

退出码
------
    0 签到成功 / 当天已签到
    2 当天已无需签到（等价成功，便于监控区分）
    3 未登录，需要人工登录一次
    4 未在界面找到签到按钮（选择器可能随版本变化，请用 --dump 排查）
    5 签到点击后未见成功反馈
    1 其他异常（程序未安装、端口起不来、超时等）

依赖：仅 Python 3.9+ 标准库 + cdp_mini.py（同目录，零第三方依赖）。
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# psutil 不是必须项：装了就用来做精确退进程，没装就用 taskkill 兜底
try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

from cdp_mini import CdpError, CdpSession, http_json, iter_page_targets
import notify

# ==========================================================================
# 配置区（一般不用改；也可通过命令行覆盖）
# ==========================================================================
APP_NAME = "WPS 灵犀.exe"
# 灵犀主程序路径。三种指定方式，优先级从高到低：
#   1) 命令行 --exe "完整路径\WPS 灵犀.exe"
#   2) 环境变量 LINGXI_EXE
#   3) 留空 -> 自动探测常见安装位置（见 find_exe）
_ENV_EXE = os.environ.get("LINGXI_EXE", "").strip()
DEFAULT_EXE = Path(_ENV_EXE) if _ENV_EXE else None
CDP_ENV = "MUSA_CDP_PORT"
DEFAULT_PORT = 19222

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
SHOT_DIR = BASE_DIR / "shots"
REPORT_DIR = BASE_DIR / "report"
HISTORY_FILE = BASE_DIR / "checkin_history.jsonl"

# 签到按钮选择器（灵犀 1.3.x）；带多个兜底，应对版本微调
SIGNIN_BTN_SELECTORS = [
    "button.task-sign-in__btn",
    ".task-sign-in__btn",
]
TASK_CENTER_PANEL = ".task-center-panel"
# 左下角积分区 —— 打开设置面板的入口（实测 class 为 footer__points footer__btn）
POINTS_ENTRY_SELECTORS = [".footer__points", ".footer__btn"]
# 设置面板里的目标菜单项
MENU_LABEL = "任务中心"

# 按钮文案（灵犀 1.3.x 界面文案，用于判读状态）
BTN_TEXT_TODO = "立即签到"
BTN_TEXT_DOING = "签到中"
BTN_TEXT_DONE_HINT = "距离下次签到"
# 已签到时按钮会带上禁用样式（实测：pointer-events-none + opacity-40）
BTN_DISABLED_CLS = "pointer-events-none"

# 超时（秒）
T_PORT_READY = 90
T_MUSE_READY = 60
T_PANEL_READY = 30
T_RESULT = 25

# 退出码分界线：<= 这个都算「签到这件事已经达成」
OK_ISH = 2

# 重要教训：不要给灵犀加 Chromium 启动开关。
# 试过 --disable-background-timer-throttling / --disable-renderer-backgrounding
# 这类参数，灵犀会直接启动失败退出（端口永远起不来）。
# 实测下来「纯隐藏窗口」就能正常工作：Chromium 的严格节流要到页面隐藏
# 5 分钟后才生效，签到流程 1~2 分钟内就跑完了，够用。
SILENT_LAUNCH_ARGS: list[str] = []

# 全局开关（由命令行参数设置）
NOTIFY_ENABLED = True          # 是否弹桌面通知
SILENT = True                  # 是否静默（隐藏控制台 + 隐藏灵犀窗口）
QUIT_AFTER_DONE = True         # 签到完成后是否退出灵犀（默认退出）
LAST_RECORD: dict = {}         # 最近一次执行的记录，供通知取文案


# ==========================================================================
# 日志
# ==========================================================================
class Log:
    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.fp = open(LOG_DIR / f"run_{dt.date.today():%Y%m%d}.log",
                       "a", encoding="utf-8")

    def __call__(self, msg: str, level: str = "INFO"):
        line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
        # pythonw / 摘掉控制台之后 sys.stdout 可能是 None，print 会炸
        if self.verbose and getattr(sys, "stdout", None) is not None:
            # 避免 Windows 控制台 GBK 打印 emoji/特殊字符炸掉
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                print(line.encode("gbk", "replace").decode("gbk"), flush=True)
        try:
            self.fp.write(line + "\n")
            self.fp.flush()
        except Exception:            # 日志已关闭等，不该影响主流程
            pass

    def close(self):
        try:
            self.fp.close()
        except Exception:
            pass


log = Log()


# ==========================================================================
# 静默运行：不弹控制台、不弹灵犀窗口
# ==========================================================================
SW_HIDE = 0
SW_SHOW = 5


def detach_console() -> bool:
    """把本进程从控制台上摘下来——计划任务拉起 python 时那个黑框随之消失。

    只影响自己：从 cmd 里手动运行时，cmd 自己的窗口不受影响。
    """
    if os.name != "nt":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not k32.GetConsoleWindow():
            return False          # 本来就是 pythonw / 无控制台，不用处理

        # 关键：必须先把「文件描述符」1/2 重定向到 nul，再释放控制台。
        # 只换 sys.stdout 是不够的——fd 1/2 仍指向那个马上要消失的控制台，
        # 之后 Popen 拉起灵犀时被它继承，灵犀会直接启动失败（端口永远起不来）。
        dn = os.open(os.devnull, os.O_RDWR)
        for fd in (1, 2):
            try:
                os.dup2(dn, fd)
            except OSError:
                pass
        if dn > 2:
            os.close(dn)
        # 再把 Python 层的输出对象也换掉，免得 print 抛 OSError
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
        return bool(k32.FreeConsole())
    except Exception:             # noqa: BLE001
        return False


def hide_app_windows(pids: list[int] | None = None) -> int:
    """隐藏属于灵犀的所有可见顶层窗口，返回本次新隐藏的窗口数。"""
    if os.name != "nt":
        return 0
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                         ctypes.c_void_p)
    want = set(pids if pids is not None else list_pids())
    hidden = 0

    def cb(hwnd, _lparam):
        nonlocal hidden
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in want and u32.IsWindowVisible(hwnd):
            u32.ShowWindow(hwnd, SW_HIDE)
            hidden += 1
        return True

    u32.EnumWindows(EnumWindowsProc(cb), 0)
    return hidden


class WindowHider(threading.Thread):
    """后台巡视线程：灵犀的窗口一露面就藏起来。

    Electron 的主窗口、弹窗都是延迟创建的，只藏一次不够，
    所以整个签到流程期间都保持巡视，直到 stop()。
    """

    def __init__(self, interval: float = 0.4):
        super().__init__(daemon=True)
        self.interval = interval
        self.hidden = 0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                n = hide_app_windows()
                if n:
                    self.hidden += n
                    log(f"已隐藏灵犀窗口（累计 {self.hidden} 次）", "DEBUG")
            except Exception:      # noqa: BLE001
                pass
            self._stop.wait(self.interval)

    def stop(self, timeout: float = 1.0):
        self._stop.set()
        self.join(timeout)


# ==========================================================================
# 进程 & 启动
# ==========================================================================
def find_exe(user_exe: str | None) -> Path:
    """定位灵犀程序：命令行 > 默认路径 > 快捷方式/常见目录探测。"""
    if user_exe:
        p = Path(user_exe)
        if not p.exists():
            raise FileNotFoundError(f"指定的程序不存在: {p}")
        return p
    if DEFAULT_EXE:
        if not DEFAULT_EXE.exists():
            raise FileNotFoundError(
                f"LINGXI_EXE 指向的程序不存在: {DEFAULT_EXE}")
        return DEFAULT_EXE

    # 兜底：常见安装位置
    home = Path(os.path.expanduser("~"))
    candidates = [
        home / "AppData/Local/Programs/lingxi-desktop/WPS 灵犀.exe",
        home / "AppData/Local/lingxi-desktop/WPS 灵犀.exe",
        Path(r"C:\Program Files\lingxi-desktop\WPS 灵犀.exe"),
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        "没找到 WPS 灵犀程序，请用 --exe \"完整路径\\WPS 灵犀.exe\" 指定。"
    )


def list_pids(process_name: str = APP_NAME) -> list[int]:
    if psutil is None:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {process_name}",
                              "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="gbk",
                             errors="replace")
        pids = []
        for line in out.stdout.splitlines():
            parts = line.split('","')
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1].strip('"')))
                except ValueError:
                    pass
        return pids
    res = []
    for p in psutil.process_iter():
        try:
            if p.name() == process_name:
                res.append(p.pid)
        except Exception:
            continue
    return res


def quit_lingxi(timeout: float = 20.0) -> None:
    """退出正在运行的灵犀（为了带上调试端口重新启动）。"""
    pids = list_pids()
    if not pids:
        log("当前没有运行中的灵犀进程")
        return

    log(f"结束已有的灵犀进程: {pids}")
    if psutil:
        procs = []
        for pid in pids:
            try:
                procs.append(psutil.Process(pid))
            except Exception:
                continue
        # 先退主进程（不带 --type= 的），避免子进程被拉起
        def is_main(pr) -> bool:
            try:
                return not any(a.startswith("--type=") for a in pr.cmdline()[1:])
            except Exception:
                return False
        for pr in sorted(procs, key=lambda x: not is_main(x)):
            try:
                pr.terminate()
            except Exception:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline and list_pids():
            time.sleep(0.5)
        for pid in list_pids():          # 顽固进程强杀
            try:
                psutil.Process(pid).kill()
            except Exception:
                pass
    else:
        subprocess.run(["taskkill", "/IM", APP_NAME, "/T", "/F"],
                       capture_output=True)
    time.sleep(1.5)
    log("灵犀已退出" if not list_pids() else "警告：仍有进程残留")


def wait_port(port: int, timeout: float = T_PORT_READY) -> None:
    """等待 CDP HTTP 端点就绪。"""
    log(f"等待 CDP 端口 {port} ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(0.6)
        try:
            s.connect(("127.0.0.1", port))
            http_json(port, "/json/version", timeout=3)
            log(f"CDP 端口 {port} 已就绪")
            return
        except Exception:
            time.sleep(1.0)
        finally:
            s.close()
    raise TimeoutError(
        f"端口 {port} 迟迟没有起来。可能程序没能正常启动，"
        f"请检查是否有其它实例在抢端口，或手动启动一次灵犀看看是否正常。"
    )


def launch(exe: Path, port: int, restart: bool = True, silent: bool = True) -> int:
    """以调试端口启动灵犀，返回新进程 pid。"""
    if restart or list_pids():
        # MUSA_CDP_PORT 只在程序启动时读取，所以必须让旧实例先退出
        quit_lingxi()

    # 同时写进程 env 与当前进程 env：前者是给子进程的，后者是双保险
    os.environ[CDP_ENV] = str(port)
    env = dict(os.environ)
    env[CDP_ENV] = str(port)
    log(f"启动灵犀并开启调试端口 {port}: {exe}")

    cmd = [str(exe)]
    if silent and SILENT_LAUNCH_ARGS:
        cmd += SILENT_LAUNCH_ARGS
        log(f"静默模式，附加启动参数: {' '.join(SILENT_LAUNCH_ARGS)}")

    # Windows：让子进程有正常窗口，并脱离当前控制台进程组（避免被父进程回收）
    startupinfo = None
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    creationflags = 0
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd,
        cwd=str(exe.parent),
        env=env,
        close_fds=True,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    log(f"已创建进程 pid={proc.pid}")

    # 兜底：有些运行环境（非交互桌面、受限令牌，比如从某些终端/脚本里拉起）
    # 下 Electron 要么启动即退出、要么活着但没开端口。
    # 这两种情况都改用 ShellExecute 再试一次——它走的是和资源管理器
    # 一样的启动路径，成功率更高，也更贴近「用户双击」的场景。
    if proc.poll() is not None:
        log("灵犀进程启动后很快退出，改用 ShellExecute 重试", "WARN")
        _shellexecute(exe)
    elif not _port_alive(port, wait=12):
        log("常规方式启动后 12 秒端口仍未就绪，改用 ShellExecute 重试", "WARN")
        _shellexecute(exe)

    wait_port(port)
    return proc.pid


def _port_alive(port: int, wait: float = 12.0) -> bool:
    """短暂探一下端口是否在 wait 秒内起来了（只探，不报错）。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            http_json(port, "/json/version", timeout=2)
            return True
        except Exception:        # noqa: BLE001
            time.sleep(1.0)
    return False


def _shellexecute(exe: Path) -> bool:
    """用 ShellExecuteW 启动程序（等价于在资源管理器里双击）。"""
    if os.name != "nt":
        return False
    try:
        # ShellExecute 会继承本进程环境，MUSA_CDP_PORT 已在 launch() 里设好
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "open", str(exe), None, str(exe.parent), 0)   # 0 = SW_HIDE
        return int(rc) > 32
    except Exception as e:                                       # noqa: BLE001
        log(f"ShellExecute 调用失败: {e}", "WARN")
        return False


# ==========================================================================
# 找到「可执行 JS 的主窗口」target
# ==========================================================================
PROBE_MUSE = """
(() => {
  const r = { hasMuse: false, hasLingxi: false, settingsKeys: null, tabs: null };
  if (typeof window.muse === 'object' && window.muse) {
    r.hasMuse = true;
    try {
      r.settingsKeys = Object.keys(window.muse.settings || {});
    } catch (e) {}
  }
  if (typeof window.lingxi === 'object' && window.lingxi) r.hasLingxi = true;
  return JSON.stringify(r);
})()
"""


def pick_main_session(port: int) -> tuple[CdpSession, dict]:
    """挑承载业务界面的 target。

    实测灵犀会有多个 page target：
      * https://lingxi.wps.cn/...        —— 真正的业务界面（积分区、设置、任务中心都在这）
      * file://.../renderer/index.html   —— 旧版设置壳窗口
      * loading.html                     —— 启动瞬间临时页，会被销毁
    因此优先选 lingxi.wps.cn 的那个；它同时也是唯一能点到签到按钮的窗口。
    """
    log("扫描 CDP targets，寻找承载业务界面的窗口")
    deadline = time.time() + T_MUSE_READY
    last: list[str] = []

    def candidates() -> list[dict]:
        ts = [t for t in iter_page_targets(port) if t.get("webSocketDebuggerUrl")]
        # 稳定排序：业务界面优先，临时加载页最后
        ts.sort(key=lambda t: (
            0 if "lingxi.wps.cn" in str(t.get("url") or "")
            else 2 if "loading.html" in str(t.get("url") or "")
            else 1
        ))
        return ts

    while time.time() < deadline:
        targets = candidates()
        if targets:
            last = [f"{t.get('title')!r} {str(t.get('url'))[:70]}" for t in targets]
        for t in targets:
            try:
                sess = CdpSession(t["webSocketDebuggerUrl"], timeout=15)
                sess.enable_dom()
                raw = sess.evaluate(PROBE_MUSE)
                info = json.loads(raw) if isinstance(raw, str) else {}
            except Exception as e:  # noqa: BLE001
                log(f"  target {str(t.get('url'))[:60]!r} 探测失败: {e}", "DEBUG")
                continue
            if info.get("hasMuse") or info.get("hasLingxi"):
                log(f"命中目标窗口: {str(t.get('url'))[:70]}")
                log(f"  muse={'有' if info.get('hasMuse') else '无'} "
                    f"settings={info.get('settingsKeys')}")
                return sess, t
            sess.close()
        time.sleep(1.5)
    raise TimeoutError(
        "没找到挂载了灵犀前端的窗口。当前 targets:\n  " + "\n  ".join(last)
    )


# ==========================================================================
# 打开任务中心
# ==========================================================================
JS_OPEN_TASK_CENTER = r"""
(async (menuLabel) => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const steps = [];
  const hasPanel = () => !!document.querySelector('.task-center-panel');
  const visible = e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const findEntry = () => {
    for (const s of ['.footer__points', '.footer__btn']) {
      const el = document.querySelector(s);
      if (el && visible(el)) return el;
    }
    return null;
  };

  if (hasPanel()) {
    steps.push('already-open');
    return JSON.stringify({ ok: true, steps });
  }

  // 步骤 0：等首屏渲染出左下角积分区（刚启动时界面可能还是空的）
  let entry = findEntry();
  for (let i = 0; i < 30 && !entry; i++) { await wait(600); entry = findEntry(); }
  if (!entry) steps.push('entry-wait-timeout');

  // 步骤 1：点积分区，弹出设置面板
  if (entry) {
    entry.scrollIntoView({ block: 'center' });
    entry.click();
    steps.push('clicked:' + String(entry.className).slice(0, 40));
    await wait(2200);
  } else {
    steps.push('no-points-entry');
  }

  // 步骤 2：面板还没开时，退而用官方 bridge 拉起设置页
  if (!hasPanel() && !document.querySelector('.p-claw-settings-dialog, .p-claw-settings')) {
    const api = (window.muse && window.muse.settings) || (window.lingxi && window.lingxi.settings);
    if (api && typeof api.openSettings === 'function') {
      try { await api.openSettings({ tab: 'taskCenter' }); steps.push('openSettings'); }
      catch (e) { steps.push('openSettings-err:' + String(e).slice(0, 60)); }
      await wait(1800);
    }
  }

  // 步骤 3：在设置面板左侧菜单里点「任务中心」（文本精确相等的叶子节点）
  if (!hasPanel()) {
    const els = [...document.querySelectorAll('*')].filter(e => {
      const t = (e.textContent || '').trim();
      return t === menuLabel && e.children.length === 0 && visible(e);
    });
    if (els.length) {
      els[0].scrollIntoView({ block: 'center' });
      els[0].click();
      steps.push('clicked-menu:' + els.length);
      await wait(2400);
    } else {
      steps.push('no-menu-item');
    }
  }

  return JSON.stringify({ ok: hasPanel(), steps });
})('任务中心')
"""

JS_FALLBACK_CLICK_ENTRY = r"""
(async () => {
  // 兜底：直接点击界面上「任务中心」字样的入口
  const hit = [...document.querySelectorAll('div,span,li,button,a,p')]
    .filter(el => {
      const t = (el.textContent || '').trim();
      return t === '任务中心' && el.offsetParent !== null;
    });
  const out = { found: hit.length, clicked: false };
  if (hit.length) {
    hit[0].click();
    out.clicked = true;
  }
  return JSON.stringify(out);
})()
"""

BTN_PROBE = r"""
(() => {
  const sels = ['button.task-sign-in__btn', '.task-sign-in__btn'];
  let el = null, usedSel = null;
  for (const s of sels) {
    el = document.querySelector(s);
    if (el) { usedSel = s; break; }
  }
  if (!el) {
    return JSON.stringify({ found: false });
  }
  return JSON.stringify({
    found: true,
    selector: usedSel,
    text: (el.textContent || '').trim(),
    disabled: !!el.disabled,
    cls: el.className,
    hasPanel: !!document.querySelector('.task-center-panel'),
    panelText: (document.querySelector('.task-center-panel')?.innerText || '').slice(0, 500),
  });
})()
"""


def wait_task_center(session: CdpSession, port: int) -> dict:
    log("打开任务中心")
    for attempt in (1, 2):
        raw = session.evaluate(JS_OPEN_TASK_CENTER, await_promise=True)
        info = json.loads(raw) if isinstance(raw, str) else {}
        log(f"  第 {attempt} 次尝试，步骤: {info.get('steps')}")
        if info.get("ok"):
            break
        time.sleep(1.5)

    deadline = time.time() + T_PANEL_READY
    while time.time() < deadline:
        probe = json.loads(session.evaluate(BTN_PROBE) or "{}")
        if probe.get("found"):
            log(f"任务中心已就绪，签到按钮文案 = {probe.get('text')!r}")
            return probe
        time.sleep(1.0)

    try:
        session.screenshot(str(SHOT_DIR / f"no_button_{dt.datetime.now():%H%M%S}.png"))
    except Exception as e:        # noqa: BLE001
        log(f"截图失败（窗口处于隐藏状态）: {e}", "WARN")
    raise RuntimeError("任务中心里没找到签到按钮（可用 --visible --dump 进一步排查）")


# ==========================================================================
# 判断 & 签到
# ==========================================================================
def judge(probe: dict) -> str:
    """根据按钮文案与样式判断当日签到状态。返回 todo/done/doing/unknown。

    实测三种界面反馈：
      未签到 -> "立即签到"（按钮可点）
      签到中 -> "签到中..."
      已签到 -> "距离下次签到 HH:MM:SS"，且 class 带 pointer-events-none/opacity-40
    """
    text = (probe.get("text") or "").strip()
    cls = probe.get("cls") or ""
    if BTN_TEXT_DOING in text:
        return "doing"
    if BTN_TEXT_DONE_HINT in text or BTN_DISABLED_CLS in cls:
        return "done"
    if BTN_TEXT_TODO in text:
        return "todo"
    return "unknown"


JS_CLICK_SIGNIN = r"""
(async () => {
  const sels = ['button.task-sign-in__btn', '.task-sign-in__btn'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el) {
      el.scrollIntoView({ block: 'center' });
      el.click();
      return JSON.stringify({ clicked: true, selector: s,
                              text: (el.textContent || '').trim() });
    }
  }
  return JSON.stringify({ clicked: false });
})()
"""


def do_checkin(session: CdpSession) -> dict:
    """点击签到并等待结果反馈。"""
    before = json.loads(session.evaluate(BTN_PROBE) or "{}")
    log(f"点击前按钮: text={before.get('text')!r} disabled={before.get('disabled')}")

    res = session.evaluate(JS_CLICK_SIGNIN, await_promise=True)
    log(f"点击结果: {res}")

    deadline = time.time() + T_RESULT
    last = None
    while time.time() < deadline:
        time.sleep(1.2)
        probe = json.loads(session.evaluate(BTN_PROBE) or "{}")
        last = probe
        state = judge(probe)
        if state in ("done", "doing"):
            log(f"签到后状态 -> {state}, 按钮文案={probe.get('text')!r}")
            return {"ok": True, "state": state, "probe": probe}
        # 也可能弹出 toast/金币动效，文案没变但面板已有「已签到」字样
        panel = (probe.get("panelText") or "")
        if "已签到" in panel:
            log("面板出现「已签到」字样，判定成功")
            return {"ok": True, "state": "done", "probe": probe}
    return {"ok": False, "state": "unknown", "probe": last or {}}


JS_TRY_QUIT = r"""
(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const tried = [];
  const roots = [window.muse, window.lingxi].filter(Boolean);
  const call = async (name, fn) => {
    try { await fn(); tried.push({ name, ok: true }); }
    catch (e) { tried.push({ name, ok: false, err: String(e).slice(0, 60) }); }
  };
  for (const root of roots) {
    const app = root.app || root.application || root.base;
    if (app) {
      for (const fn of ['quit', 'exit', 'closeApp', 'close']) {
        if (typeof app[fn] === 'function') await call('app.' + fn, () => app[fn]());
      }
    }
    for (const fn of ['quit', 'exit']) {
      if (typeof root[fn] === 'function') await call('root.' + fn, () => root[fn]());
    }
  }
  try { window.close(); tried.push({ name: 'window.close', ok: true }); }
  catch (e) { tried.push({ name: 'window.close', ok: false }); }
  await wait(600);
  return JSON.stringify({ tried });
})()
"""

# ==========================================================================
# 收尾：签到完成后退出灵犀
# ==========================================================================
WM_CLOSE = 0x0010


def post_close_to_lingxi() -> int:
    """给灵犀的所有顶层窗口发 WM_CLOSE，让它走自己的关闭流程（比强杀体面）。

    窗口此刻是隐藏的，但隐藏窗口照样收得到消息。
    """
    if os.name != "nt":
        return 0
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                         ctypes.c_void_p)
    want = set(list_pids())
    sent = 0

    def cb(hwnd, _lparam):
        nonlocal sent
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in want:
            u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            sent += 1
        return True

    u32.EnumWindows(EnumWindowsProc(cb), 0)
    return sent


def quit_after_done(timeout: float = 8.0) -> bool:
    """签到完成后把灵犀整个退出，而不是缩在托盘里。"""
    pids = list_pids()
    if not pids:
        log("灵犀当前没有在运行")
        return True

    log(f"准备退出灵犀，当前进程: {pids}")
    sent = post_close_to_lingxi()
    log(f"已向 {sent} 个窗口发送 WM_CLOSE")

    deadline = time.time() + timeout
    while time.time() < deadline and list_pids():
        time.sleep(0.5)
    if not list_pids():
        log("灵犀已自行退出")
        return True

    # 不少 Electron 应用收到 WM_CLOSE 只是缩到托盘，进程还在 —— 那就结束进程
    log("WM_CLOSE 之后进程仍在（多半是缩到托盘了），改为结束进程", "WARN")
    quit_lingxi()
    ok = not list_pids()
    if not ok:
        log("警告：仍有灵犀进程残留", "WARN")
    return ok


JS_CLOSE_PANEL = r"""
(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const hasPanel = () => !!document.querySelector('.task-center-panel');
  if (!hasPanel()) return JSON.stringify({ closed: true, how: 'none' });

  // 1) 优先用官方 bridge 关掉设置面板
  const api = (window.muse && window.muse.settings) ||
              (window.lingxi && window.lingxi.settings);
  for (const fn of ['closeSettings', 'close', 'hideSettings']) {
    if (api && typeof api[fn] === 'function') {
      try { await api[fn](); await wait(600); } catch (e) {}
      if (!hasPanel()) return JSON.stringify({ closed: true, how: 'api:' + fn });
    }
  }

  // 2) 点关闭按钮（class 里带 close 的小图标）
  const btns = [...document.querySelectorAll('[class*="close"],[class*="Close"]')]
    .filter(e => e.offsetParent !== null && e.getBoundingClientRect().width > 0 &&
                 e.getBoundingClientRect().width < 80);
  if (btns.length) {
    btns[btns.length - 1].click();
    await wait(700);
    if (!hasPanel()) return JSON.stringify({ closed: true, how: 'click' });
  }

  // 3) 最后按 ESC
  document.dispatchEvent(new KeyboardEvent('keydown',
    { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
  await wait(700);
  return JSON.stringify({ closed: !hasPanel(), how: 'esc' });
})()
"""


def close_panel(session: CdpSession) -> None:
    """收尾：把任务中心面板关掉，免得下次打开灵犀停在设置页。尽力而为。"""
    try:
        res = session.evaluate(JS_CLOSE_PANEL, await_promise=True)
        log(f"关闭任务中心面板: {res}")
    except Exception as e:        # noqa: BLE001
        log(f"关闭面板失败（不影响结果）: {e}", "WARN")


def try_quit_via_bridge(session: CdpSession) -> None:
    """先礼后兵：让灵犀自己调退出接口，失败了后面还有 WM_CLOSE 和结束进程。"""
    try:
        res = session.evaluate(JS_TRY_QUIT, await_promise=True)
        log(f"尝试调用灵犀退出接口: {res}")
    except Exception as e:        # noqa: BLE001
        log(f"调用退出接口失败（后续用 WM_CLOSE 兜底）: {e}", "WARN")


# ==========================================================================
# 排障导出
# ==========================================================================
DUMP_JS = r"""
(() => {
  const out = {
    url: location.href,
    title: document.title,
    hasMuse: typeof window.muse === 'object' && !!window.muse,
    museTop: window.muse ? Object.keys(window.muse) : null,
    museBase: window.muse && window.muse.base ? Object.keys(window.muse.base) : null,
    museSettings: window.muse && window.muse.settings ? Object.keys(window.muse.settings) : null,
    hasLingxi: typeof window.lingxi === 'object' && !!window.lingxi,
    lingxiTop: window.lingxi ? Object.keys(window.lingxi) : null,
    hasCenterPanel: !!document.querySelector('.task-center-panel'),
    signBtn: !!document.querySelector('.task-sign-in__btn'),
    loggedInGuess: null,
    bodyHead: document.body ? document.body.innerText.slice(0, 400) : null,
    // 界面上所有含「任务中心」「签到」的可点击元素，用于 UI 点击兜底
    taskEntries: [...document.querySelectorAll('div,span,li,button,a,p,li')]
      .map(el => ({
        tag: el.tagName.toLowerCase(),
        cls: (el.className && el.className.toString().slice(0, 60)) || '',
        text: (el.textContent || '').trim().slice(0, 30),
        visible: !!(el.offsetParent || el.clientWidth),
      }))
      .filter(x => x.visible && (x.text.includes('任务中心') || x.text.includes('签到')))
      .slice(0, 30),
    iframes: [...document.querySelectorAll('iframe')].map(f => f.src).slice(0, 10),
  };
  return JSON.stringify(out);
})()
"""

# dump 时逐个试「打开任务中心」的候选 API，并把成败记录下来
DUMP_TRY_OPEN = r"""
(async () => {
  const tries = [];
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const present = () => !!document.querySelector('.task-center-panel');
  const attempt = async (name, fn) => {
    try {
      const r = await fn();
      tries.push({ name, ok: true, resp: JSON.stringify(r ?? null).slice(0, 200) });
    } catch (e) {
      tries.push({ name, ok: false, err: String(e).slice(0, 200) });
    }
    await wait(700);
  };

  const m = window.muse, l = window.lingxi;
  if (m && m.settings) {
    for (const tab of ['taskCenter', 'task_center', 'points']) {
      await attempt('muse.settings.openSettings:' + tab,
        () => m.settings.openSettings({ tab }));
      if (present()) break;
    }
  }
  if (!present() && l && l.settings) {
    for (const tab of ['taskCenter', 'task_center']) {
      await attempt('lingxi.settings.openSettings:' + tab,
        () => l.settings.openSettings({ tab }));
      if (present()) break;
    }
  }
  if (!present() && m && m.settings && typeof m.settings.openSettingsModal === 'function') {
    await attempt('muse.settings.openSettingsModal',
      () => m.settings.openSettingsModal({ activeSetting: 'taskCenter' }));
  }
  return JSON.stringify({ ok: present(), tries });
})()
"""


def dump(session: CdpSession, target: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1) 按正式流程打开任务中心，看哪一步奏效
    try:
        open_res = session.evaluate(JS_OPEN_TASK_CENTER, await_promise=True)
    except Exception as e:  # noqa: BLE001
        open_res = json.dumps({"error": str(e)})

    # 2) 收集界面结构
    info = json.loads(session.evaluate(DUMP_JS) or "{}")
    info["target"] = {k: target.get(k) for k in ("id", "title", "url", "type")}
    try:
        info["tryOpen"] = json.loads(open_res) if isinstance(open_res, str) else open_res
    except json.JSONDecodeError:
        info["tryOpen_raw"] = str(open_res)[:500]

    # 3) 若此刻已经能进到任务中心，顺带把签到按钮状态记下来
    try:
        probe = json.loads(session.evaluate(BTN_PROBE) or "{}")
        if probe.get("found"):
            info["signBtnNow"] = probe
    except Exception:
        pass

    shot = SHOT_DIR / f"dump_{now}.png"
    try:
        session.screenshot(str(shot))
        info["screenshot"] = str(shot)
    except Exception as e:  # noqa: BLE001
        info["screenshotError"] = str(e)

    out_file = REPORT_DIR / f"dump_{now}.json"
    out_file.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已导出排查信息: {out_file}")

    summary_keys = [
        "url", "hasMuse", "museTop", "museBase", "museSettings",
        "hasLingxi", "lingxiTop", "hasCenterPanel", "signBtn",
        "iframes", "screenshot",
    ]
    for k in summary_keys:
        log(f"  {k}: {str(info.get(k))[:300]}")
    log(f"  可用入口候选 {len(info.get('taskEntries') or [])} 个:")
    for e in (info.get("taskEntries") or [])[:12]:
        log(f"     <{e['tag']}> {e['text']!r} class={e['cls']!r}")
    try:
        log(f"  打开任务中心结果: {info['tryOpen']}")
    except KeyError:
        pass
    return info


# ==========================================================================
# 记录
# ==========================================================================
def append_history(record: dict) -> None:
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        log(f"写历史记录失败（不影响主流程）: {e}", "WARN")


# ==========================================================================
# 桌面通知
# ==========================================================================
def notify_result(code: int, record: dict) -> None:
    """按本次结果弹一条系统通知。签到达成报喜，没达成也让人知道要处理。"""
    if not NOTIFY_ENABLED:
        log("通知已关闭（--no-notify）")
        return

    detail = (record.get("detail") or "").strip()
    result = record.get("result")

    # 排障类用法不打扰
    if result in ("dump_ok", "dry_run_todo"):
        return

    if code == 0 and result == "signed":
        title, msg = "灵犀签到成功", detail or "今日积分已领取"
    elif code == 2:
        title = "灵犀今日已签到"
        msg = "无需重复操作" + (f"（{detail}）" if detail else "")
    elif code == 3:
        title, msg = "灵犀自动签到未完成", "未检测到登录状态，请手动登录一次"
    elif code == 4:
        title, msg = "灵犀自动签到未完成", "没找到签到按钮，界面可能已改版"
    elif code == 5:
        title, msg = "灵犀自动签到未完成", "点了签到但没看到成功反馈，请手动确认"
    else:
        title = "灵犀自动签到未成功"
        msg = f"退出码 {code}：{detail[:60] or '见 logs 目录'}"

    log(f"弹出通知: {title} / {msg}")
    try:
        notify.notify(title, msg)
    except Exception as e:        # noqa: BLE001
        log(f"通知发送异常（不影响退出码）: {e}", "WARN")


# ==========================================================================
# 主流程
# ==========================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="灵犀专业版每日自动签到",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", help="WPS 灵犀.exe 的完整路径")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="CDP 调试端口")
    ap.add_argument("--dry-run", action="store_true", help="只判断不点签到")
    ap.add_argument("--dump", action="store_true", help="导出排查信息后退出")
    ap.add_argument("--no-restart", action="store_true",
                    help="不主动重启灵犀（需要已有实例已开调试端口）")
    ap.add_argument("--timeout", type=float, default=T_PORT_READY)
    # 下面两个是给定时任务用的：登录后等系统就绪，失败了自己重试，
    # 这样计划任务可以直接调 python，不必再包一层 .bat
    ap.add_argument("--wait", type=float, default=0,
                    help="开跑前先等待的秒数（开机后等网络和托盘就绪）")
    ap.add_argument("--retries", type=int, default=1, help="失败后总共尝试几次")
    ap.add_argument("--retry-interval", type=float, default=180,
                    help="两次尝试之间等待的秒数")
    ap.add_argument("--visible", action="store_true",
                    help="不隐藏灵犀窗口、不摘控制台（排障观察用）")
    ap.add_argument("--no-notify", action="store_true", help="不弹桌面通知")
    ap.add_argument("--no-quit", action="store_true",
                    help="签到完成后不退出灵犀（默认会退出）")
    ap.add_argument("--test-notify", action="store_true",
                    help="只发一条测试通知后退出")
    args = ap.parse_args(argv)

    global NOTIFY_ENABLED, SILENT, QUIT_AFTER_DONE
    NOTIFY_ENABLED = not args.no_notify
    SILENT = not args.visible
    QUIT_AFTER_DONE = not args.no_quit

    if args.test_notify:
        ok = notify.notify("灵犀自动签到 · 测试", "看到这条说明通知通道正常")
        print("通知已发送" if ok else "通知发送失败")
        return 0 if ok else 1

    if SILENT:
        # 越早摘掉控制台越好：连 --wait 那几十秒都不会有黑框
        log.verbose = False
        detach_console()

    if args.wait > 0:
        log(f"按要求先等待 {args.wait:.0f} 秒再开始")
        time.sleep(args.wait)

    # 单次失败就到这里为止（dump / dry-run 这类交互用法不该反复重试）
    if args.retries <= 1:
        code = run_once(args)
        notify_result(code, LAST_RECORD)
        log.close()
        return code

    # 重试期间保持安静：只有最后一次（成功提前结束的那次也算）才弹通知
    last = 1
    for i in range(1, args.retries + 1):
        log(f"===== 第 {i}/{args.retries} 次尝试 =====")
        last = run_once(args)
        if last <= OK_ISH:
            break
        if i < args.retries:
            log(f"本次未成功（退出码 {last}），等待 {args.retry_interval:.0f} 秒后重试")
            time.sleep(args.retry_interval)
    notify_result(last, LAST_RECORD)
    log.close()
    return last


def run_once(args) -> int:
    """执行一次完整的签到流程，返回退出码。"""
    started = time.time()
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    code = 1
    hider: WindowHider | None = None
    session: CdpSession | None = None
    record: dict = {"time": dt.datetime.now().isoformat(timespec="seconds"),
                    "result": None, "detail": ""}
    try:
        exe = find_exe(args.exe)
        log(f"灵犀程序: {exe}")

        need_launch = True
        if args.no_restart:
            try:
                http_json(args.port, "/json/version", timeout=2)
                need_launch = False
                log(f"复用已开启的调试端口 {args.port}")
            except Exception:
                log("指定端口不通，仍需重启", "WARN")

        if need_launch:
            launch(exe, args.port, restart=not args.no_restart, silent=SILENT)

        # 静默模式下开窗即隐藏：从这里一直守到流程结束
        if SILENT:
            hider = WindowHider()
            hider.start()
            log("已启动窗口隐藏巡视")

        session, target = pick_main_session(args.port)

        if args.dump:
            dump(session, target)
            session.screenshot(str(SHOT_DIR / f"dump_{dt.datetime.now():%H%M%S}.png"))
            session.close()
            log("--dump 模式结束")
            record.update(result="dump_ok", detail="只导出排查信息，未签到")
            code = 0
            return 0

        probe = wait_task_center(session, args.port)
        state = judge(probe)
        log(f"当日签到状态判定: {state}")

        if args.dump:
            dump(session, target)

        if state == "done":
            record.update(result="already", detail=probe.get("text", ""))
            log("今天已签到，无需操作")
            code = 2
        elif state == "doing":
            record.update(result="in_progress", detail=probe.get("text", ""))
            log("签到进行中，稍后再确认即可")
            code = 2
        elif state == "todo":
            if args.dry_run:
                log("--dry-run：本应点击签到，已跳过")
                record.update(result="dry_run_todo", detail=probe.get("text", ""))
                code = 0
            else:
                out = do_checkin(session)
                try:
                    session.screenshot(
                        str(SHOT_DIR / f"after_{dt.datetime.now():%H%M%S}.png"))
                except Exception as e:    # noqa: BLE001
                    log(f"截图失败（窗口处于隐藏状态，不影响结果）: {e}", "WARN")
                if out["ok"]:
                    record.update(result="signed", detail=str(out["probe"].get("text")))
                    log("签到完成")
                    code = 0
                else:
                    record.update(result="sign_failed",
                                  detail=str(out["probe"].get("text")))
                    log("签到后未检测到成功状态，请查看 shots/ 截图", "ERROR")
                    code = 5
        else:
            record.update(result="unknown", detail=str(probe.get("text")))
            log("无法判读签到状态，请运行 --dump 导出界面信息", "ERROR")
            code = 4

        # 收尾：把任务中心面板关掉，下次打开灵犀不会停在设置页
        close_panel(session)

        # 签到这件事已经办完，按用户要求把灵犀整个退出，不留在托盘里。
        # --dump / --dry-run 属于排障用法，界面还得留着看，所以不退出。
        # code <= OK_ISH 表示「签到这件事已经达成」；失败时留着灵犀，
        # 方便你自己进去手动点一下。排障模式同理。
        if QUIT_AFTER_DONE and code <= OK_ISH and not (args.dry_run or args.dump):
            try_quit_via_bridge(session)
            session.close()
            session = None
            quit_after_done()
        else:
            log(f"本次不退出灵犀（退出码 {code}"
                + ("，排障模式" if (args.dry_run or args.dump) else "") + "）")
            session.close()
            session = None
    except FileNotFoundError as e:
        record.update(result="error", detail=str(e))
        log(str(e), "ERROR")
        code = 1
    except TimeoutError as e:
        record.update(result="timeout", detail=str(e)[:300])
        log(str(e), "ERROR")
        code = 1
    except CdpError as e:
        record.update(result="cdp_error", detail=str(e)[:300])
        log(f"CDP 通信失败: {e}", "ERROR")
        code = 1
    except Exception as e:  # noqa: BLE001
        record.update(result="exception", detail=str(e)[:300])
        log(f"未预期异常: {e}\n{traceback.format_exc()}", "ERROR")
        code = 1
    finally:
        if hider is not None:
            hider.stop()
        if session is not None:
            try:
                session.close()
            except Exception:      # noqa: BLE001
                pass
        record["elapsed"] = round(time.time() - started, 1)
        global LAST_RECORD
        LAST_RECORD = record
        append_history(record)
        # 注意：这里不要 log.close()。通知是在 run_once 之后才发的，
        # 提前关掉日志会让 notify_result 里的 log() 抛异常，
        # 进而把退出码搅成 1、通知也发不出去。统一由 main 收尾时关闭。
        log(f"结束，退出码={code}，用时 {record['elapsed']}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
