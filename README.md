# 灵犀专业版 · 每日自动签到

> 每天开机登录后，自动打开 WPS 灵犀 → 进入「任务中心」→ 判断当天是否已签到 → 未签到则点击签到领取积分。
> 幂等设计：一天运行多次也只会签到一次。

## 它做了什么 / 没做什么

**做了什么**

1. 检测灵犀是否在运行。
2. 以内置调试端口重新启动灵犀（详见下方"为什么需要重启一次"）。
3. 通过调试端口连到灵犀界面：
   - 点左下角**积分区**（如 `4944`）打开设置面板；
   - 点左侧菜单**「任务中心」**；
   - 读取签到按钮文案。
4. 判断当日状态：
   - `立即签到` → 未签到，点击签到；
   - `距离下次签到 HH:MM:SS` → 今天已签到，**不做任何操作**；
   - `签到中...` → 进行中，等待结束。
5. 结果写入 `checkin_history.jsonl`，截图写入 `shots/`。

**没做什么**

- 不修改灵犀的任何文件，不注入代码，不伪造签到请求 —— 它只是替你点了那一下按钮。
- 不读取、不保存你的账号密码或 Cookie。
- 不会连续重复签到（每次点击前都会重新判读按钮状态）。

## 为什么需要重启一次灵犀

灵犀桌面端是 Electron 应用，它的启动代码里内置了这样一段：

```js
const port = parseInt(process.env.MUSA_CDP_PORT ?? "", 10)
if (port > 0) app.commandLine.appendSwitch("remote-debugging-port", port)
```

也就是说，**只要在它启动时给它环境变量 `MUSA_CDP_PORT`，它自己就会打开标准调试端口**，我们通过这个正规通道去操作界面。

麻烦在于 Electron 有单实例锁：已经有一个实例在跑时再双击，第二个会立刻退出，环境变量自然也就不会生效。所以脚本采取"先退出旧实例 → 再带端口重启"的策略。

发生在刚开机登录的时候，代价可以忽略；登录状态存在本地用户数据里，**重启不会掉登录**。

> 如果你不想让它重启已有的灵犀，可以用 `--no-restart`，但前提是当时已经有一个带调试端口的实例在跑。

## 文件说明

| 文件 | 作用 |
|---|---|
| `lingxi_checkin.py` | **主脚本**。零第三方依赖，Python 3.9+ 直接跑 |
| `cdp_mini.py` | 极简 Chrome DevTools Protocol 客户端（纯标准库实现） |
| `register_task.py` | 注册 / 注销 / 立即触发定时任务（需要 pywin32） |
| `1_验证一次.bat` | 双击跑一次只读排障，导出界面信息，**不会真签到** |
| `2_注册开机自启.bat` | 注册"登录时自动签到"任务 |
| `3_注销自动签到.bat` | 注销定时任务，干净退出 |
| `test_cdp_mini.py` | 调试协议客户端的自测，含分片 / 大包 / 错误处理用例 |
| `diagnostics/` | 排查用的临时脚本与输出，平时不用管 |
| `logs/` | 运行日志 |
| `shots/` | 关键步骤截图，出问题时看这里 |
| `report/` | `--dump` 导出的排查信息 |
| `checkin_history.jsonl` | 每次运行的结果流水 |

## 常用命令

```bat
:: 手动跑一次（已签到则跳过，不会重复）
python lingxi_checkin.py

:: 只看状态不点按钮，用来确认今天签到了没
python lingxi_checkin.py --dry-run

:: 导出界面结构排查用（不签到）
python lingxi_checkin.py --dump

:: 灵犀装在别的路径
python lingxi_checkin.py --exe "D:\your\path\WPS 灵犀.exe"

:: 换个端口（默认 19222）
python lingxi_checkin.py --port 19999

:: 模拟定时任务的完整行为：先等 60 秒，失败最多重试 3 次
python lingxi_checkin.py --wait 60 --retries 3
```

## 定时任务

已经注册好了：**任务名「灵犀每日自动签到」，触发器「每次登录时，延迟 60 秒」**。
任务直接调用 Python，参数是 `--wait 60 --retries 3 --port 19222`；

> 之所以不再包一层 `.bat`：批处理在代码页、`%~dp0`、变量展开上坑太多，
> 等待与重试交给脚本自己处理更可靠。

```bat
python register_task.py --status     :: 查看状态与上次结果
python register_task.py --run        :: 立即触发一次试试
python register_task.py --uninstall  :: 注销

2_注册开机自启.bat                   :: 双击注册（内含 schtasks 兜底）
3_注销自动签到.bat                   :: 双击注销
```

> 注：有的机器安全策略会禁止 `schtasks.exe`。这种环境请用 `register_task.py`
> （走任务计划的 COM 接口），效果完全一样。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 签到成功 |
| 2 | 当天已签到 / 签到中（等价成功，便于监控区分） |
| 3 | 未登录（预留） |
| 4 | 没找到签到按钮 —— 界面可能改版了，用 `--dump` 看 report |
| 5 | 点了签到但没检测到成功反馈 —— 看 `shots/after_*.png` |
| 1 | 环境类错误：程序没装、端口起不来、超时等 |

定时任务里退出码 ≤ 2 视为成功，其它会再重试，最多 3 次。

## 实测记录（2026-09-29）

在本机跑通的完整链路，耗时约 12 秒：

```
结束已有灵犀进程 -> 带 MUSA_CDP_PORT=19222 重启（端口 2 秒就绪）
命中 target: https://lingxi.wps.cn/_desktop/lingxi/
打开任务中心: clicked footer__points -> clicked 菜单「任务中心」
签到按钮文案: "距离下次签到 11:52:15"  ->  判定 done  ->  退出码 2
```

当天已签到时脚本不会点击，所以把它丢到早上自动跑是安全的。

## 出问题怎么办

1. **看日志**：`logs\run_日期.log`。
2. **看截图**：`shots\` 里保留了打开面板后 / 点完签到后的画面。
3. **导出现场**：`python lingxi_checkin.py --dump`，然后把 `report\dump_*.json` 发出来。
4. **界面改版了**：最常见的情况。表现是退出码 4。解决办法是在 `--dump` 的 JSON 里找新的 `footer__*` 入口和菜单名，改 `lingxi_checkin.py` 里的这几个常量即可：
   ```python
   POINTS_ENTRY_SELECTORS = [".footer__points", ".footer__btn"]
   MENU_LABEL = "任务中心"
   SIGNIN_BTN_SELECTORS = ["button.task-sign-in__btn", ".task-sign-in__btn"]
   ```
5. **跑起来了但没签到**：大概率是当天已经签过。按钮会显示"距离下次签到 12:07:06"这样的文案，脚本据此判定为已完成，这是预期行为。

## 维护建议

- 灵犀大版本升级后，建议手动跑一次 `python lingxi_checkin.py --dry-run` 确认仍能识别界面。
- 不需要它的时候，跑一下 `register_task.py --uninstall` 就干净退出，不留任何后台进程。
