# 端到端启动总纲：Dashboard + LM Studio + TORCS + AI Bot

本文把 [`dashboard-startup-guide.md`](dashboard-startup-guide.md)、[`torcs-granite-quickstart.md`](torcs-granite-quickstart.md)、[`wslg-black-screen-recovery.md`](wslg-black-screen-recovery.md) 串成一条完整链路，并补充了在 Windows + WSL2 混合环境下才会踩到的坑。

## 怎么用这份文档

每个代码块开头都标了 **【WSL 终端】** 或 **【Windows PowerShell】**，只有这两种：

- **【WSL 终端】**：在 VSCode 里打开的 WSL 终端（提示符长得像 `user@HOST:~/F1-simulator$`）里粘贴运行。
- **【Windows PowerShell】**：在 Windows 的 PowerShell 窗口里粘贴运行，不是 WSL 终端。

**看到哪个标签，就去对应的终端粘贴整块代码，不用做任何修改。** 全文不再出现 `wsl bash -lc "..."` 这种嵌套写法。

## 哪些步骤必须终端/手动，哪些可以在 Dashboard 网页里点按钮

Dashboard 网页是 midware 提供出来的监控/遥控台，不是游戏本身，也不是大模型本身。三件事天生不可能在网页里做，必须终端或手动：

- **第 1 步**：启动 midware 本身——没起 midware 就没有这个网页可打开。
- **第 3 步的第 1 小步**：打开 LM Studio App——独立 Windows 程序，网页帮不了忙。
- **第 4、5 步**：启动 TORCS 窗口、在游戏内选赛道/进 Quick Race——TORCS 是独立进程，Dashboard 只能被动收它的 UDP 遥测。

除此之外，下面两步有网页按钮可以直接替代命令行，效果完全一样：

- **第 3 步第 4 小步**（把 midware 指向 LM Studio）：Dashboard 的 **Commentary 标签页**里填 Base URL / Model 点保存。
- **第 6 步**（启动 AI Bot）：Dashboard 顶部 Bot 卡片的 **Start ai_bot.py** 按钮，比终端手动启动更好——网页按钮启动的进程 midware 才能正确追踪 pid 和日志。

---

## 0. 前置检查（每次开始前 10 秒确认一下）

**【WSL 终端】**

```bash
curl -s -m 3 http://127.0.0.1:8880/api/health >/dev/null && echo "midware 还活着" || echo "midware 没起"
```

- 输出 `midware 还活着` → 直接跳到第 4 步（开 TORCS）。
- 输出 `midware 没起` → 从第 1 步开始。

---

## 1. 启动 midware（Dashboard 后端）

**【WSL 终端】**

```bash
cd ~/F1-simulator
nohup midware/.venv/bin/python -m midware.app > /tmp/f1-main-dashboard.log 2>&1 &
disown
echo "已启动，PID: $!"
```

验证：

**【WSL 终端】**

```bash
sleep 2
curl -s http://127.0.0.1:8880/api/health
```

看到一大段 JSON（而不是报错）就算成功。

> 为什么不直接用一行 `nohup ... & echo $!`：如果这条命令是从 **Windows PowerShell** 里通过 `wsl bash -lc "..."` 发过去的，PowerShell 的转义规则会把 `$!`、`&` 吃掉，导致看起来启动成功、实际日志是空文件。**在 WSL 终端里直接跑就没有这个问题**，这也是本文档统一让你在 WSL 终端里操作的原因之一。

---

## 2. 打开 Dashboard 网页

**【Windows PowerShell】**

```powershell
Start-Process "http://127.0.0.1:8880/static/dashboard.html"
```

注意是 `/static/dashboard.html`，不是 `/dashboard`（后者 404）。

---

## 3.（可选）启用 AI 功能：接 LM Studio

只看遥测数据可以跳过这一整节；要用 Race Engineer 问答 / Granite 点评就必须做。

### 3.1 打开 LM Studio（手动，Windows 桌面操作）

打开 LM Studio → 左侧 **Local Server**（`>_`）→ 选好 Granite 模型（如 `granite-4.1-8b`），状态 `READY` → 点 **Start Server**。

### 3.2 查 Windows 主机 IP

**【WSL 终端】**

```bash
ip route | grep default
```

输出类似 `default via 172.21.160.1 dev eth0`，记下这个 IP，下面步骤里的 `172.21.160.1` 都要换成你实际查到的。

### 3.3 测通 LM Studio

**【WSL 终端】**（把 `172.21.160.1` 换成你 3.2 查到的 IP）

```bash
curl -s -m 5 http://172.21.160.1:1234/v1/models
```

能看到 JSON 里有 `granite` 字样就算通。

### 3.4 把 midware 指向 LM Studio

**方式 A —— 网页操作（推荐）**：打开 Dashboard 的 **Commentary 标签页**，填 Base URL（`http://172.21.160.1:1234/v1`）和 Model（`granite-4.1-8b`），点保存。

**方式 B —— 【WSL 终端】**（跟方式 A 二选一，不用两个都做）：

```bash
curl -s -X POST http://127.0.0.1:8880/api/config/api \
  -H "Content-Type: application/json" \
  -d '{"base_url": "http://172.21.160.1:1234/v1", "model": "granite-4.1-8b"}'
```

返回 `{"ok":true}` 就算成功。

### 3.5 冒烟测试

**【WSL 终端】**

```bash
curl -s -m 30 -X POST http://127.0.0.1:8880/api/engineer/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "say hello in five words"}'
```

返回里有 `answer` 字段，就说明 midware → LM Studio 通了。

⚠️ 这一步配置存在 midware 进程的内存里，**每次重启 midware 都要重新做一次 3.4**。

---

## 4. 启动 TORCS

**【WSL 终端】**

```bash
cd ~/F1-simulator
export TORCS_HOME=~/F1-simulator
nohup bash torcs_launcher.sh > /tmp/f1-torcs.log 2>&1 &
disown
echo "TORCS 启动中，PID: $!"
```

等 10~15 秒后检查：

**【WSL 终端】**

```bash
tail -n 20 /tmp/f1-torcs.log
```

### 如果日志提示"No TORCS window was detected"

先不要慌，很可能窗口其实存在，只是 xdotool 在 WSL 内部检测失败。去 **Windows 侧**确认真实窗口：

**【Windows PowerShell】**

```powershell
Get-Process | Where-Object { $_.MainWindowTitle -ne "" } | Select-Object ProcessName, MainWindowTitle, Id
```

找 `msrdc` 进程，标题里带 `torcs-bin`。如果有，说明窗口真实存在，只是被挡住/没置顶，把下面的 `<PID>` 换成查到的 Id 后激活它：

**【Windows PowerShell】**

```powershell
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class Win32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
'@
$p = Get-Process -Id <PID>
[Win32]::ShowWindow($p.MainWindowHandle, 9) | Out-Null
[Win32]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
```

### 如果确实是黑屏/没画面（Get-Process 里也找不到 torcs-bin 窗口）

按 [`wslg-black-screen-recovery.md`](wslg-black-screen-recovery.md) 的结论，直接重启整个 WSL 图形会话：

**【Windows PowerShell】**

```powershell
wsl.exe --shutdown
```

⚠️ **这会杀掉 midware、TORCS 和 WSL 里所有其他进程**，执行前务必先确认没有其他人在用 WSL 里的其他东西。重启后要重新走第 1、3 步（LM Studio 配置也要重发一次），再重新跑本步骤。

---

## 5. 在 TORCS 窗口里手动操作

这一步必须人工点（无法从终端脚本化）：

1. 选一条赛道
2. 进入 **Quick Race**
3. 车手列表确认用的是 `scr_server`
4. 开始比赛，停在 **`Initializing Driver scr_server 1...`** 界面等 bot 连接

---

## 6. 启动 AI Bot

**方式 A —— 网页操作（推荐）**：Dashboard 顶部 Bot 卡片点 **Start ai_bot.py** 按钮。这样 midware 能正确追踪 pid 和日志。

**方式 B —— 【WSL 终端】**（网页打不开、或者要脚本化批量操作时用）：

```bash
cd ~/F1-simulator
nohup python3 ai_bot.py --bot --granite > /tmp/f1-ai-bot.log 2>&1 &
disown
echo "Bot 启动中，PID: $!"
```

**已知坑**：Python 的 stdout 重定向到文件后是全缓冲的，`tail /tmp/f1-ai-bot.log` 可能长时间看不到任何内容——这是正常的，不代表没启动，也不代表没连上 TORCS。不要用日志文件判断是否连上，改用第 7 步的接口。

---

## 7. 验证 AI 真的在开车

**【WSL 终端】**

```bash
curl -s http://127.0.0.1:8880/api/bot/status
```

```bash
curl -s http://127.0.0.1:8880/api/health | python3 -c 'import sys,json; print(json.load(sys.stdin)["model"]["scheduler"])'
```

- 第一条返回里 `status.connected == true` 且 `speed_kmh > 0`：车在跑。
- 第二条 `completed` 数字随时间持续增长：Granite 在持续被调用。

**注意**：如果 `ai_bot.py` 是用第 6 步「方式 B」从终端手动起的（不是点 Dashboard 按钮），`curl http://127.0.0.1:8880/api/bot/process/status` 会显示 `running: false, pid: null`——这只是 midware 进程管理器不知道这个外部进程的存在，不影响 bot 通过 UDP 实际连接和驾驶，Dashboard 顶部的连接状态依然会正常显示 connected。

---

## 方法二：一次性丢给 AI 的提示词

把下面这段话原样发给 Claude Code（或类似的能跑终端命令的 AI 助手），让它按本文档一次性走完全流程：

> 请按仓库里 `docs/full-stack-e2e-startup.md` 的步骤，帮我把 Dashboard、LM Studio 连接、TORCS、AI Bot 全部启动起来：
> 1. 先检查 midware 是否已经在跑（第 0 步），没起就在 WSL 终端里后台启动。
> 2. 帮我在浏览器打开 Dashboard。
> 3. 我会自己去开 LM Studio，开好后告诉你；你负责测通、把 midware 指过去、跑一次冒烟测试。
> 4. 启动 TORCS；如果检测不到窗口，先在 Windows 侧查真实窗口是否存在并尝试激活；如果确认黑屏，跟我确认后再执行 `wsl.exe --shutdown` 重启 WSL（这会杀掉所有进程，重启后要重新走 1-3 步）。
> 5. TORCS 起来后提醒我手动选赛道、进 Quick Race、选 `scr_server`，停在 `Initializing Driver` 界面告诉你。
> 6. 我确认后，你启动 AI Bot（优先用 Dashboard 的 Start ai_bot.py 按钮，做不到再用终端）。
> 7. 最后用 `/api/bot/status` 和 `/api/health` 里的 scheduler 计数验证车真的在跑、AI 真的在被调用，把结果汇报给我。
>
> 涉及 `wsl.exe --shutdown` 这种会杀掉所有 WSL 进程的操作，执行前必须先问我确认。

这段提示词的关键在于：把"人工必须操作的节点"（LM Studio 开服、TORCS 菜单选择、WSL 重启前的确认）显式标出来，AI 会在这些点上停下来等你，而不是卡住或者瞎猜。
