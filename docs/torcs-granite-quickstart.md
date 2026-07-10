# 启动 TORCS 并连接 LM Studio（Granite）— 复用指南

记录方向四（AI 赛车 Bot）从零开始跑起来的完整流程：启动 LM Studio、验证连通、打开 TORCS 画面、跑 bot 并确认真的在调用 Granite。

## 前提

- LM Studio 装在 Windows 主机上（不是 WSL 里）。
- TORCS 仓库在 WSL 内：`~/F1-simulator`。
- 用 `ai_bot.py`，**不要用** `torcs-granite/bot/`——后者的 `TelemetryCollector.start()` 和握手逻辑有已知 bug，连不上真实 TORCS。

## 第一步：启动 LM Studio 本地服务器（Windows 侧）

1. 打开 LM Studio → 左侧 **Local Server**（`>_` 图标）。
2. 顶部下拉选好已加载的 Granite 模型（如 `granite-4.1-8b`），确认状态是 `READY`，不是灰色。
3. 点 **Start Server**。
4. 记下页面上 **Reachable at** 那一行地址，例如：
   - `http://127.0.0.1:1234`（默认，仅本机回环）
   - 或 `http://10.x.x.x:1234`（如果 Server Settings 开了"Serve on Local Network"，会显示 Windows 主机的局域网 IP）

端口一般都是 **1234**，不用改；变化的通常是 IP。这个 IP 每次重启 LM Studio/Windows 都可能变，用之前先在这个页面确认一下当前值。

## 第二步：WSL 里验证能连上

```bash
cd ~/F1-simulator
python3 lmstudio_smoke_test.py
```

`telemetry_common.connect_openai_compatible_model()` 自带兜底链：先试 `127.0.0.1:1234` 直连，不通再自动走 `powershell.exe` 代理到 Windows 主机。这台机器是 Windows 10 + WSL2 默认 NAT 网络（没配 mirrored networking），所以直连大概率失败、走 powershell 兜底属于正常现象。

如果 LM Studio 显示的是局域网 IP（如 `10.x.x.x:1234`）而不是 `127.0.0.1`，可以跳过兜底、直连更快：

```bash
export TORCS_AI_BASE_URL=http://10.x.x.x:1234/v1
python3 lmstudio_smoke_test.py
```

看到模型返回一句话回复，说明这一步通了。

## 第三步：启动 TORCS 并看到画面

TORCS 用老式固定管线 OpenGL，和 WSLg 默认硬件加速 GL 不兼容，会出现"进程正常起、有声音/日志，但没有窗口"的情况。解决办法是强制软件渲染：

```bash
cd ~/F1-simulator
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
TORCS_HOME=~/F1-simulator bash torcs_launcher.sh
```

`torcs_launcher.sh` 已经内置了这两个环境变量，但脚本里 `TORCS_HOME` 默认写死成队友的路径（`/home/yejian/torcs`），**必须**用 `TORCS_HOME=~/F1-simulator` 覆盖，否则会去错目录。

启动后选一个赛道进 Quick Race，画面会停在类似：

```
Loading Driver scr_server 1...
Initializing Driver scr_server 1...
```

这是正常的——TORCS 在等外部 bot 客户端通过 SCR 协议连进来，不要关掉这个窗口，直接进行下一步。

## 第四步：启动 bot，连 TORCS + Granite

另开一个终端：

```bash
cd ~/F1-simulator
python3 ai_bot.py --bot --granite
```

**关键点**：`--bot` 参数必须加。不加的话 `ai_bot.py` 只会跑内置自测试（打印一堆 `... OK` 和 `All tests passed.` 然后直接退出），根本不会连 TORCS——这是最容易踩的坑。

参数说明：
- `--bot`：进入真正的驾驶循环，默认连 `localhost:3001`（TORCS scr_server 默认端口，不用改）
- `--granite`：启用 Granite 策略层；不加则固定用 NORMAL 策略、不连 LM Studio
- `--strategy XXX`：手动指定初始策略（ATTACK/NORMAL/DEFEND/SAVE_FUEL/PIT），一般不需要

## 第五步：确认真的连上了（不是只是"没报错"）

终端里应该看到：

1. 启动时一行：
   ```
   Connecting to TORCS at localhost:3001  strategy=NORMAL  granite=True…
   Identified! Entering drive loop. Press Ctrl-C to stop.
   ```
   如果是 `granite=False`，或上面有一行 `[warn] Could not connect to Granite (...)`，说明退回了固定策略，没连上 LM Studio，回到第二步重新排查。

2. 之后每隔约 **5 秒**一行（`ai_bot.py` 里 `_STRATEGY_INTERVAL = 5.0`）：
   ```
   [Granite] ATTACK — clear track ahead
   ```

   这个值是实测调过的：本地 `granite-4.1-8b` 的单次请求耗时约 1.4-2.3 秒（985 字符 prompt、80 max_tokens），5 秒间隔留了约 2 倍余量，落在计划书 ~0.1-1Hz 的目标区间内。超时时间 `_GRANITE_TIMEOUT` 也从 30s 收紧到 10s。`LatestTaskRunner`（[telemetry_common.py:232](../telemetry_common.py#L232)）只保留最新任务，就算某次请求还没返回、下一个 5 秒 tick 就到了，也只会覆盖待处理任务，不会堆积。

   如果换了更大/更慢的模型，或者换成走 powershell 代理（比直连慢），重新用下面的方法量一次延迟，再按"约 2 倍最坏延迟"的比例调 `_STRATEGY_INTERVAL`：

   ```python
   # 临时测速，测完删掉，不要提交
   import time
   from telemetry_common import connect_openai_compatible_model, chat_completion_text
   from ai_bot import _build_strategy_prompt, _GRANITE_MAX_TOK
   conn = connect_openai_compatible_model()
   prompt = _build_strategy_prompt({"speed_x": 180, "fuel": 60, "damage": 0, "track_pos": 0,
                                     "gear": 5, "race_pos": 3, "dist_raced": 1000,
                                     "track": [150.0]*19, "opponents": [200.0]*36})
   t0 = time.monotonic()
   chat_completion_text(conn, messages=[{"role": "user", "content": prompt}],
                         temperature=0.1, max_tokens=_GRANITE_MAX_TOK, timeout=30.0)
   print(time.monotonic() - t0)
   ```

也可以从另一个终端做进程级验证，不依赖读终端文字：

```bash
# 确认 bot 进程带了 --granite
ps aux | grep ai_bot

# 确认和 TORCS 的 UDP 连接是活的（PID 换成实际值）
ss -tunp | grep <PID>
# 应该能看到: udp ESTAB 127.0.0.1:xxxx 127.0.0.1:3001
```

## 常见坑速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 跑完直接回到 `$` 提示符，只有 `All tests passed.` | 忘了加 `--bot` | 重新执行 `python3 ai_bot.py --bot --granite` |
| TORCS 窗口卡在 `Initializing Driver...` 不动 | bot 还没连上/没启动 | 检查第四步是否执行、端口是否 3001 |
| LM Studio 连不上 | Windows 10 默认 NAT 网络，WSL `localhost` 打不到 Windows 主机 | 用 powershell 兜底（自动）或 `TORCS_AI_BASE_URL` 指到 LM Studio 显示的局域网 IP |
| TORCS 有声音/日志但没画面 | WSLg 默认 GL 路径和 TORCS 老式 OpenGL 不兼容 | `LIBGL_ALWAYS_SOFTWARE=1` + `GALLIUM_DRIVER=llvmpipe`，仍不行就 `wsl.exe --shutdown` 后重开（见 [wslg-black-screen-recovery.md](wslg-black-screen-recovery.md)） |
| 想用 `torcs-granite/bot/` 里更"规范"的目录结构 | 该模块 `TelemetryCollector.start()` 绑定端口和握手格式都有 bug，连不上真实 TORCS | 先别用，等移植完 `ai_bot.py` 的通信层再说 |
