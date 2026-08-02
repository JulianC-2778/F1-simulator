# L4 / L5 人工检测详细步骤

[testing-plan.md](testing-plan.md) 定义了五层测试，L0–L3 已由 `tools/run_tests.sh` 全自动化。
本文是剩下两层——**L4 外部依赖**和 **L5 端到端验收**——的逐项操作手册。

每一项按 `前置 → 操作 → 判定标准 → 失败时查什么` 组织。判定标准都是**可观察的具体输出**，
不要用"看起来没报错"当通过。

---

## 0. 开始之前

### 0.1 环境现状（2026-07-26 实测）

这台 WSL（Ubuntu，唯一发行版）当前**缺少下列外部依赖**，相关检测项在补齐之前无法执行：

| 缺失 | 影响的检测项 | 补齐命令 |
| --- | --- | --- |
| `node` / `npm`（Linux 版；现在只有 Windows 侧 `/mnt/d/tavern/npm`） | 5.9、5.10、L0 的 JS 静态检查 | `curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh \| bash` → `nvm install --lts` |
| `xdotool` | `torcs_launcher.sh` 的窗口定位（TORCS 仍能启动，只是不自动摆正） | `sudo apt-get install -y xdotool` |
| `parecord` / `pactl` | 4.5 语音输入 | `sudo apt-get install -y pulseaudio-utils` |
| `faster-whisper` | 4.5 语音输入 | `pip install -r requirements-voice.txt` |
| `kokoro` + `kokoro-v1_0.pth` + `voices/*.pt` | 4.4 TTS、5.10 语音播报 | `pip install -r requirements-tts.txt` + 见 4.4 的下载命令 |

> npm 必须是 **Linux 版**。WSL 里调用 Windows 的 npm 装 Electron 会报
> `UNC paths are not supported` / `Cannot find module 'C:\Windows\install.js'`。

### 0.2 TORCS 二进制只在运行目录

```bash
ls ~/projects/for_summer_project/BUILD/bin/torcs   # ✅ 存在
ls ~/summer-project/F1-simulator/BUILD/bin/torcs   # ❌ 不存在
```

所以 **L5 必须在 `~/projects/for_summer_project/` 下跑**，而代码改在仓库
`~/summer-project/F1-simulator/`。每次 L5 之前先同步并校验：

```bash
cp ~/summer-project/F1-simulator/{ai_bot.py,track_model.py} ~/projects/for_summer_project/
md5sum ~/summer-project/F1-simulator/ai_bot.py ~/projects/for_summer_project/ai_bot.py
# 两行哈希必须一致，否则你测的是旧代码
```

也可以让仓库里的启动脚本指向运行目录的构建产物：

```bash
TORCS_HOME=~/projects/for_summer_project bash ~/summer-project/F1-simulator/torcs_launcher.sh
```

### 0.3 每次开测前清场

```bash
pkill -f 'midware.app'; pkill -f 'ai_bot.py'; pkill -f 'tts_server.py'; pkill -f torcs
ss -tulnp | grep -E '8880|3101|3001|8766|8881'    # 应该没有输出
```

3101 只能有一个监听者（`TelemetryService`）。如果这里看到别的进程占着，后面的遥测检测一定会假失败。

### 0.4 通用观察工具：WebSocket 监听器

L5 的解说、工程师回答、错误都走 `ws://127.0.0.1:8880/ws`。开一个终端挂着，
比盯 GUI 可靠得多（依赖 `websocket-client`，`requirements-core.txt` 已含）：

```bash
cd ~/summer-project/F1-simulator && source .venv/bin/activate
python - <<'EOF'
import json, websocket
ws = websocket.create_connection("ws://127.0.0.1:8880/ws")
print("connected, watching...")
while True:
    m = json.loads(ws.recv())
    t = m.get("type")
    if t in ("telemetry_update",):        # 太吵，过滤掉
        continue
    print(f"[{t}] source={m.get('source')} seq={m.get('sequence')} "
          f"{str(m.get('content') or m.get('message') or '')[:100]}")
EOF
```

---

## L4 — 外部依赖检测

### 4.1 Granite / LM Studio 连通

**前置**：LM Studio 装在 Windows 侧。

**操作**

1. Windows 打开 LM Studio → 左侧 `Local Server`（`>_` 图标）。
2. 顶部下拉选中已加载的 Granite 模型（如 `granite-4.1-8b`），状态必须是 **READY**（不是灰色）。
3. 点 `Start Server`，记下 `Reachable at` 那一行地址（端口一般固定 1234，IP 每次重启可能变）。
4. WSL 侧验证：

```bash
cd ~/summer-project/F1-simulator && source .venv/bin/activate
python lmstudio_smoke_test.py
```

**判定标准**

- 打印连接 banner（含实际 base_url 和 model 名）
- 打印 `Model response:` + 模型的一句英文回复

**失败时查什么**

- `telemetry_common.connect_openai_compatible_model()` 有兜底链：先试 `127.0.0.1:1234` 直连，
  不通再走 `powershell.exe` 代理到 Windows 主机。**这台机器是 WSL2 默认 NAT 网络，
  直连失败、走 powershell 兜底是正常现象**，不算 bug。
- 想跳过兜底（更快）：LM Studio 开 `Serve on Local Network`，然后
  `export TORCS_AI_BASE_URL=http://10.x.x.x:1234/v1` 再跑。
- 确认 midware 用的是同一配置：`curl -s 127.0.0.1:8880/api/health | python -m json.tool`
  看 `model.base_url` 和 `model.model`。

---

### 4.2 模型延迟是否在预算内

**为什么要测**：`ai_bot.py` 的 `_STRATEGY_INTERVAL = 5.0` 是按"约 2 倍最坏延迟"定的。
换模型、换网络路径（直连 vs powershell 代理）都会让这个假设失效。

**操作**

```bash
cd ~/summer-project/F1-simulator && source .venv/bin/activate
python - <<'EOF'
import statistics, time
from telemetry_common import connect_openai_compatible_model, chat_completion_text
from ai_bot import _build_strategy_prompt, _GRANITE_MAX_TOK

conn = connect_openai_compatible_model()
prompt = _build_strategy_prompt({
    "speed_x": 180, "fuel": 60, "damage": 0, "track_pos": 0, "gear": 5,
    "race_pos": 3, "dist_raced": 1000,
    "track": [150.0] * 19, "opponents": [200.0] * 36,
})
times = []
for i in range(5):
    t0 = time.monotonic()
    chat_completion_text(conn, messages=[{"role": "user", "content": prompt}],
                         temperature=0.1, max_tokens=_GRANITE_MAX_TOK, timeout=30.0)
    dt = time.monotonic() - t0
    times.append(dt)
    print(f"  run {i+1}: {dt:.2f}s")
print(f"\nmedian={statistics.median(times):.2f}s  max={max(times):.2f}s")
print(f"建议 _STRATEGY_INTERVAL >= {max(times) * 2:.1f}s（当前 5.0s）")
EOF
```

**判定标准**

- 5 次全部成功返回
- `max` ≤ 2.5s → 当前 5.0s 间隔安全，通过
- `max` > 2.5s → 不通过，按输出的建议值调 `ai_bot.py` 的 `_STRATEGY_INTERVAL`，
  并同步到运行目录（见 0.2）

**注**：即使某次请求超时，`LatestTaskRunner`（[telemetry_common.py](../telemetry_common.py)）
只保留最新任务，不会堆积——但间隔太短会让策略层长期落后于赛况。

---

### 4.3 Model Broker 串行化与优先级

**为什么要测**：所有 feature 的模型调用都走 `ModelBroker`，默认并发 1。要确认
engineer（最高优先级）不会被 coach/commentary 的排队饿死，且 bot 的实时控制**完全不等模型**。

**前置**：4.1 通过；`python -m midware.app` 已启动。

**操作**

```bash
# 终端 A：挂着 0.4 节的 WebSocket 监听器

# 终端 B：先看基线统计
curl -s 127.0.0.1:8880/api/health | python -c "import sys,json;print(json.load(sys.stdin)['model']['scheduler'])"

# 同时打三个请求（engineer 最后发，但应该最先返回）
curl -s -X POST 127.0.0.1:8880/api/commentary/manual -H 'Content-Type: application/json' -d '{"prompt":"warmup"}' &
curl -s 127.0.0.1:8880/api/coach/dashboard > /dev/null &
sleep 0.2
time curl -s -X POST 127.0.0.1:8880/api/engineer/ask -H 'Content-Type: application/json' \
     -d '{"question":"Should I brake earlier into turn one?"}' | python -m json.tool
wait

# 再看统计
curl -s 127.0.0.1:8880/api/health | python -c "import sys,json;print(json.load(sys.stdin)['model']['scheduler'])"
```

**判定标准**

- engineer 返回 `{"ok": true, "answer": "..."}`，`answer` 是有意义的英文句子
- scheduler 统计里 `completed` 增加，`failed` 和 `dropped` 保持 0
- 任意时刻 `active` ≤ 1（并发 1 生效）
- 终端 A 能看到 `[ai_start] source=engineer` → `[ai_done] source=engineer`

**失败时查什么**

- `dropped > 0`：队列满（默认 16）或任务过期，说明请求打得太密
- `failed > 0`：看 `last_error`，通常是模型侧超时
- engineer 返回 502：模型不可达，回 4.1

---

### 4.4 TTS 服务（Kokoro）

**前置**：权重不在仓库里，必须先下载（约 330 MB）：

```bash
cd ~/summer-project/F1-simulator && source .venv/bin/activate
pip install -r requirements-tts.txt
python -c "
from huggingface_hub import hf_hub_download
hf_hub_download('hexgrad/Kokoro-82M', 'kokoro-v1_0.pth', local_dir='.')
for v in ['af_heart', 'bm_lewis', 'bm_george']:
    hf_hub_download('hexgrad/Kokoro-82M', f'voices/{v}.pt', local_dir='.')
"
ls -lh kokoro-v1_0.pth voices/
```

**操作**

```bash
# 终端 A
python tts_server.py
# 启动日志应出现：Loading Kokoro model from ... on cpu|cuda  →  Kokoro model loaded

# 终端 B
curl -s 127.0.0.1:8881/health | python -m json.tool
curl -s 127.0.0.1:8881/voices | python -m json.tool
curl -s -X POST 127.0.0.1:8881/tts -H 'Content-Type: application/json' \
     -d '{"text":"Box this lap, box this lap.","voice":"bm_lewis"}' -o /tmp/tts_test.wav
ls -lh /tmp/tts_test.wav && file /tmp/tts_test.wav
```

**判定标准**

- `/health` → `{"ok": true, "model_loaded": true}`（`model_loaded: false` 说明权重没加载上）
- `/voices` → `downloaded` 数组里能看到你下载的那几个 voice id
- `/tmp/tts_test.wav` 大小 > 50 KB，`file` 识别为 `RIFF ... WAVE audio`，播放能听清

**接入 midware**（5.10 的前置）：

```bash
curl -s -X POST 127.0.0.1:8880/api/config/tts -H 'Content-Type: application/json' \
     -d '{"enabled": true, "url": "http://127.0.0.1:8881/tts", "voice": "bm_lewis"}'
curl -s 127.0.0.1:8880/api/health | python -c "import sys,json;print(json.load(sys.stdin)['tts'])"
# 应为 {'enabled': True, 'url': 'http://127.0.0.1:8881/tts'}
```

**失败时查什么**

- `503 Model not loaded` → 启动日志里找 `FileNotFoundError`，权重路径必须是仓库根的 `kokoro-v1_0.pth`
- `400 Voice 'x' not downloaded` → `voices/x.pt` 没下
- CPU 合成很慢是正常的（启动日志会 warn `CUDA not available`）

---

### 4.5 语音输入（faster-whisper + PulseAudio）

**前置**

```bash
sudo apt-get install -y pulseaudio-utils
pip install -r requirements-voice.txt
```

**操作**

```bash
# 1. 确认 WSLg 把 Windows 麦克风桥进来了，源名应为 RDPSource
pactl list sources short

# 2. 录 3 秒真实语音，检查不是静音
parecord --device=RDPSource /tmp/mic_test.wav & sleep 3; kill %1
python - <<'EOF'
import array, math, wave
w = wave.open("/tmp/mic_test.wav")
assert w.getsampwidth() == 2, "expected 16-bit PCM"
samples = array.array("h", w.readframes(w.getnframes()))
rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0
print(f"samples={len(samples)}  rms={rms:.0f}")
EOF

# 3. 端到端转写
cd ~/summer-project/F1-simulator && source .venv/bin/activate
python -c "
import voice_input
print('mic_available:', voice_input.mic_available())
print('text:', repr(voice_input.record_and_transcribe_blocking('说完按 Enter：')))
"
```

**判定标准**

- `pactl list sources short` 输出里含 `RDPSource`
- 说话时 rms **> 5000**（本项目环境实测说话约 8000+，噪声底接近 0）；rms 接近 0 说明没录到
- `mic_available: True`
- 转写结果是你说的英文内容（模型是 `base.en`，**只支持英文**，说中文必然失败——这是设计如此）

**失败时查什么**

- `voice_input.py` 的所有失败都返回 `""` 而不抛异常，所以"空字符串"要靠打印的原因行区分：
  - `[VoiceInput] Microphone not available (...)` → parecord 缺失或源名不对
  - `[VoiceInput] No speech recognized. Recording kept at: /tmp/xxx.wav` → 录到了但没识别，
    去听那个保留的 wav，能区分"录进去是静音"和"Whisper 没认出来"
- 源名不是 `RDPSource` 时用 `export TORCS_ENGINEER_VOICE_DEVICE=<实际名>`
- 详细排查见 [voice-input-setup.md](voice-input-setup.md)

---

## L5 — 端到端验收

### 5.0 启动方式

**前置检查表（缺一不可）**

- [ ] L0–L3 全绿（`bash tools/run_tests.sh --service`）
- [ ] 4.1 通过（Granite 可达）
- [ ] `ai_bot.py` / `track_model.py` 已同步到运行目录且 md5 一致（0.2 节）
- [ ] 端口已清场（0.3 节）

**方式 A：图形控制面板（推荐）**

```bash
cd ~/summer-project/F1-simulator && source .venv/bin/activate
python launcher_gui.py
```

一个窗口里分组管理全部模块，每个都有「启动 / 停止 / 查看日志」，并每秒轮询
`/api/health`、`/api/features/status`、`/api/bot/status` 显示实时状态：

| 分组 | 模块 |
| --- | --- |
| TORCS 本体（**两种模式互斥**） | `torcs_human`（人类驾驶，带 UDP 3101 遥测环境变量）/ `torcs_scr`（Bot 模式） |
| Feature 3 | `midware`（共享中枢，其他都依赖它）/ `overlay_commentary` |
| Feature 1 | `engineer_gui` / `overlay_engineer` |
| Feature 2 | `feature2` |
| Feature 4 | `ai_bot` |
| 可选 | `tts` |

面板管不了的两件事（必须手动）：TORCS 菜单里选 Quick Race / 车手 / `scr_server 1`；
以及人类驾驶模式和 SCR 模式的互斥切换。

**方式 B：手动多终端**

```bash
# T1 共享中枢（必须最先起）
cd ~/summer-project/F1-simulator && source .venv/bin/activate && python -m midware.app

# T2 TORCS —— 人类驾驶模式（测 5.4–5.7）
cd ~/projects/for_summer_project
TORCS_PLAYER_UDP_HOST=127.0.0.1 TORCS_PLAYER_UDP_PORT=3101 \
  bash ~/summer-project/F1-simulator/torcs_launcher.sh
#   或 Bot 模式（测 5.1–5.3）：./BUILD/bin/torcs -ver 2013

# T3 Bot（仅 Bot 模式）
cd ~/projects/for_summer_project && python ai_bot.py --bot --granite

# T4 Overlay
cd ~/summer-project/F1-simulator/overlay-app && npm start

# T5 WebSocket 监听器（0.4 节）
```

> ⚠️ TORCS 一次只能跑一个 race session，**人类驾驶模式和 SCR Bot 模式互斥**。
> 建议分两轮走查：第一轮 Bot 模式做 5.1–5.3，第二轮人类驾驶做 5.4–5.10。

---

### 5.1 SCR 握手

**前置**：TORCS 已用 SCR 模式启动，Quick Race 里选了 `scr_server 1`，画面停在
`Initializing Driver scr_server 1...`（这是正常的，它在等 bot 连进来，别关）。

**操作**

```bash
cd ~/projects/for_summer_project && python ai_bot.py --bot --granite
```

`--bot` 必须加。**不加只会跑内置自测然后退出**（打印一堆 `... OK` 和 `All tests passed.`），
这是最常踩的坑。

**判定标准**（三个独立证据，都要看）

1. bot 终端打印：
   ```
   Connecting to TORCS at localhost:3001  strategy=NORMAL  granite=True…
   Identified! Entering drive loop. Press Ctrl-C to stop.
   ```
2. TORCS 画面从 `Initializing Driver...` 进入实际比赛，车在动
3. 进程级验证（不依赖读终端文字）：
   ```bash
   ps aux | grep ai_bot                 # 确认带了 --bot --granite
   ss -tunp | grep <bot 的 PID>          # 应见 udp ESTAB 127.0.0.1:xxxxx 127.0.0.1:3001
   curl -s 127.0.0.1:8880/api/bot/status | python -m json.tool
   #   status.connected 必须为 true，health 为 "healthy"（不是 "disconnected"）
   #   details.heartbeat_age_s 应 < 3（BotStatusReporter 每秒 POST 一次）
   ```

**失败时查什么**

- TORCS 卡在 `Initializing Driver` 不动 → bot 没启动或端口不是 3001
- `connected: false` 但 bot 终端说 Identified → 心跳 POST 打不到 midware，
  检查 midware 是否在 8880，以及 bot 是否在同一台

---

### 5.2 Granite 策略层生效

**判定标准**

- 启动行显示 `granite=True`。若显示 `granite=False`，或上方有
  `[warn] Could not connect to Granite (...)`，说明退回固定策略了 → 回 4.1
- 之后**约每 5 秒**一行：
  ```
  [Granite] ATTACK — clear track ahead
  ```
  策略必须是 `ATTACK/NORMAL/DEFEND/SAVE_FUEL/PIT` 之一，reason 是人能读懂的短句
- `curl -s 127.0.0.1:8880/api/bot/status` 里 `status.strategy` 随之变化
- `/api/health` 的 `model.scheduler.completed` 持续增长，`failed`/`dropped` 不涨

**关键**：策略切换有防抖——同一个新策略要**连续两次**被提议才真正切换
（`_next_debounced_strategy`）。所以看到 `[Granite] ATTACK` 输出但 `status.strategy`
还是 `NORMAL`，只出现一次的话是**正常的**，不是 bug。

---

### 5.3 安全过滤优先于模型

**为什么要测**：这是 Feature 4 的核心安全约束——转向/油门/制动和 `safety_filter()`
**不等待模型也不等待 Middleware**。模型挂了车也必须能开。

**操作**（不用真撞车，直接断模型）

1. 车在跑的状态下，Windows 侧 LM Studio 点 `Stop Server`
2. 观察 bot 终端和 TORCS 画面 30 秒
3. 重新 `Start Server`，观察是否自动恢复

**判定标准**

- 断开模型后：**车继续正常行驶**，不停顿、不打摆、不退出
- bot 终端出现超时/连接失败的 warn，但驱动循环不中断
- 策略退回固定值（NORMAL）继续跑
- 恢复模型后，`[Granite] ...` 输出自动重新出现，无需重启 bot

**补充**（真实赛况下顺带确认，不用刻意制造）

- 燃油 < 5 → 策略被强制为 `PIT`（覆盖 ATTACK）
- damage ≥ 9500 → 强制 `DEFEND`
- 8000 ≤ damage < 9500 或 fuel < 15 → `ATTACK` 被降级为 `NORMAL`

这些规则的逻辑已由 L1 的 `python ai_bot.py` 自测覆盖，L5 只需确认**真车上也生效**。

---

### 5.4 人类驾驶遥测入库

**前置**：切到人类驾驶模式（先停掉 SCR 模式的 TORCS）。

**操作**

```bash
# 一边开车，一边在另一个终端连续采样
for i in $(seq 1 10); do
  curl -s 127.0.0.1:8880/api/telemetry | \
    python -c "import sys,json;t=json.load(sys.stdin).get('telemetry') or {};print(t.get('seq'), t.get('speedX'), t.get('lap'))"
  sleep 1
done
curl -s 127.0.0.1:8880/api/health | python -c "import sys,json;h=json.load(sys.stdin);print(h['telemetry']['ok'], h['telemetry']['ingestor'])"
```

**判定标准**

- `seq` 严格递增，`speedX` 随油门/刹车变化（不是恒定值）
- `/api/health` 的 `telemetry.ok` 为 `true`
- `ingestor.received_frames` 持续增长，`parse_failures` 保持 0
- `telemetry.status.is_stale` 为 `false`（停车超过 3 秒会变 true，属正常）

**失败时查什么**

- `received_frames` 一直是 0 → TORCS 没往 3101 发。人类驾驶模式必须带上
  `TORCS_PLAYER_UDP_HOST/PORT` 环境变量（`launcher_gui.py` 的 `torcs_human` 已内置）
- `parse_failures` 在涨 → 字段格式对不上，看 `ingestor.last_error`

---

### 5.5 Feature 2 遥测看板

**操作**

```bash
curl -s '127.0.0.1:8880/api/coach/dashboard?window_seconds=6&history_seconds=16' | python -m json.tool | head -40
```

开车过程中隔 10 秒再打一次。也可以起独立服务 `python midware/feature2_service.py`（端口 8766）看网页。

**判定标准**

- HTTP 200，返回 dict
- 有遥测时 `/api/health` 里 coach 的 `lifecycle` 从 `degraded` 变 `running`，
  `last_error` 从 `"No telemetry frames available yet."` 变空
- 两次采样的内容随驾驶变化（issue 列表、数值不是固定的）
- 独立服务模式下页面自动刷新

---

### 5.6 Feature 1 工程师问答与输出路由

**为什么要测**：工程师的回答必须路由到工程师窗口，**不能串到解说字幕**。
这靠 WebSocket 消息里的 `source` 字段区分。

**操作**

1. 终端挂 0.4 节的 WebSocket 监听器
2. 起工程师窗口：`python chat_engineer_gui.py`（或 CLI `python chat_engineer.py`）
3. 问一句，例如 `Am I braking too late?`

**判定标准**

- 监听器依次打印：
  ```
  [ai_start] source=engineer ...
  [ai_done]  source=engineer <英文回答>
  ```
  `source` 必须是 `engineer`，**不是** `commentary`
- 回答出现在工程师窗口里，**解说 Overlay 的字幕不受影响**
- `curl -s 127.0.0.1:8880/api/engineer/history` 里 `messages` 条数增加
- 回答内容和当前车况相关（提到速度/挡位/轮胎等实际遥测），不是泛泛而谈——
  说明 `car_state` 真的注入了 prompt
- `curl -s -X POST 127.0.0.1:8880/api/engineer/clear` 后 history 清空

**失败时查什么**

- 回答与车况无关 → 检查 5.4 是否通过（没有遥测时会用 `empty_car_state()`）
- 端口冲突：`chat_engineer.py` 会先探测 midware 的 `/api/telemetry`，探测得到就不自己绑 UDP。
  如果它绑了 3101，说明 midware 没起来——这会和主服务抢端口

---

### 5.7 自动解说与去重

**操作**

1. WebSocket 监听器挂着
2. 正常驾驶 2–3 分钟，制造事件（超车、出弯、碰撞、进维修区）
3. 也可手动触发一次：
   ```bash
   curl -s -X POST 127.0.0.1:8880/api/commentary/manual \
        -H 'Content-Type: application/json' -d '{"prompt":"describe the current situation"}'
   # 立即返回 {"ok":true,"queued":true}，实际内容走 WebSocket
   ```
4. 查看事件与配置：
   ```bash
   curl -s 127.0.0.1:8880/api/events/recent | python -m json.tool | head -30
   curl -s 127.0.0.1:8880/api/commentary/config | python -m json.tool
   ```

**判定标准**

- 事件发生后监听器出现 `[ai_start] source=commentary` → `[ai_done] source=commentary`
- 解说内容与实际赛况对得上（提到的名次/圈数/事件是真的）
- **无明显重复**：同一事件不会在 `dedupe_seconds` 内被说第二遍
- 长度大体不失控（但注意：`max_words` 目前只是存在 `CommentaryConfig` 里的一个
  配置值，并**没有**被写进真正发给模型的 prompt 正文，也没有任何生成后截断逻辑
  ——也就是说它对输出长度没有代码层面的强制力，纯粹靠 persona 里"one short
  sentence"这类措辞和模型自身习惯撑着。若观察到明显超长解说，这是已知缺口，
  不是这次走查该负责修的回归，详见 `docs/commentary_test_matrix.md` 第 4 节和
  `evaluation/commentary/scripts/word_count.py` 的实测口径）
- 空闲时有 baseline 解说（按 `baseline_interval`），但不会淹没事件解说

**失败时查什么**

- 完全没有解说 → 确认 commentary feature 是 enabled 的
  （`curl -s 127.0.0.1:8880/api/features/status`），以及有遥测（5.4）
- 重复严重 → 调 `POST /api/commentary/config` 的 `dedupe_seconds` / `event_cooldown`
- 解说滞后很久 → 看 `model.scheduler.queued`，可能被 coach/engineer 排队堵住

---

### 5.8 WebSocket 断线重连

**操作**

```bash
# Overlay 正常显示字幕时
pkill -f 'midware.app'
# 等 10 秒，观察 Overlay
cd ~/summer-project/F1-simulator && source .venv/bin/activate && python -m midware.app
# 再等 10 秒
```

**判定标准**

- midware 停掉后，Overlay 在 3 秒内显示 `Connection lost`
- Overlay **不崩溃、不弹错误框**，持续每 3 秒重试
- midware 恢复后，Overlay 自动变回 `Waiting for commentary...`，**无需手动重启 Overlay**
- 恢复后新解说能正常显示
- `curl -s 127.0.0.1:8880/api/health | ... ['overlay']['ws_clients']` 恢复为 ≥ 1

同样的方式测工程师 Overlay（`npm run start:engineer`）。

---

### 5.9 Overlay 外观与设置窗口

**前置**：Linux 版 node 已装（0.1 节）；`cd overlay-app && npm install` 成功，
无 `npm error`（`npm warn deprecated ...` 不算失败）。

**操作与判定**

| 检查 | 判定标准 |
| --- | --- |
| 无后端启动 | `npm start` → 窗口出现并显示 `Connection lost`，每 3 秒重连 |
| 窗口形态 | 约 900×160 px，靠近主屏底部居中，**无标题栏 / 工具栏 / 关闭按钮**，透明、置顶 |
| 设置入口 | 点字幕面板里的小设置按钮，或菜单 `TORCS AI Overlay → Settings` |
| 设置项完整 | 连接（WS URL / 重连间隔 / ping 间隔）、模型 API、解说人设、语音、自动解说、数据源与操作 六组俱全 |
| 配置回读 | 点 `Reload`，能从 midware 拉到后端配置 |
| 持久化 | 改一项 → 保存 → 重启 Overlay → 设置仍在 |

消息到 UI 的映射（对照 [../overlay-app/TESTING.md](../overlay-app/TESTING.md)）：

| 后端消息 | 期望字幕 |
| --- | --- |
| `connected` | `Waiting for commentary...` |
| `ai_start` | `Generating captions...` |
| `token` | 不立即变化（缓冲中） |
| `ai_done` | 最终英文解说 |
| `error` | `Commentary error: ...` |
| `telemetry_update` / `event_detected` | 忽略 |

**后端没就绪时的替代方案**：TESTING.md 第 10 节给了一个 mock WebSocket 服务器，
能在不跑 TORCS 的情况下把上面这张表全走一遍。测 UI 行为时优先用它，比真实赛况可控得多。

---

### 5.10 TTS 语音播报

**前置**：4.4 通过且已 `POST /api/config/tts` 打开。

**操作**

1. Overlay 设置窗口 → 勾选 `Enable voice commentary` → 选 voice → 点 `Test Voice` → `Save Voice`
2. 触发一次解说（5.7 的手动触发即可）

**判定标准**

- `Test Voice` 能听到测试句
- 真实解说时：`ai_start` **打断**上一句未念完的语音；`ai_done` 到达后完整念一遍最终文本
- `Connection lost`、`Waiting for commentary...`、`Commentary error` 这三类状态文字
  **不应该被念出来**
- 同一句解说不会念两遍

**失败时查什么**

- voice 下拉里只有 `System default` → 装原生兜底：
  `sudo apt-get install -y speech-dispatcher espeak-ng`，然后 `spd-say "TORCS voice test"`
  能出声再重启 Overlay
- 有字幕没声音 → 先单测 4.4 的 `/tts` 接口，区分是 TTS 服务问题还是 Overlay 播放问题

---

## 走查顺序与耗时

一次完整 L4+L5 建议按这个顺序，避免来回切换 TORCS 模式：

| 阶段 | 内容 | 约耗时 |
| --- | --- | ---: |
| 1 | 0.1–0.3 环境补齐与清场 | 5 min（首次装依赖更久） |
| 2 | 4.1 → 4.2 → 4.3 | 8 min |
| 3 | 4.4 → 4.5（可选，不做就标 SKIP） | 10 min |
| 4 | **TORCS SCR 模式**：5.1 → 5.2 → 5.3 | 15 min |
| 5 | 切人类驾驶模式：5.4 → 5.5 → 5.6 → 5.7 | 20 min |
| 6 | 5.8 → 5.9 → 5.10 | 10 min |

## 结果记录

每项记 `PASS / FAIL / SKIP`，FAIL 和 SKIP 必须写原因：

```text
日期：2026-__-__    执行人：
代码版本：git rev-parse --short HEAD =
同步校验：md5sum 一致 ☐

L4  4.1 Granite 连通      [ ]  备注：
    4.2 延迟预算          [ ]  median=__s  max=__s
    4.3 Broker 串行化     [ ]  completed=__ failed=__ dropped=__
    4.4 TTS               [ ]
    4.5 语音输入          [ ]  rms=__

L5  5.1 SCR 握手          [ ]
    5.2 Granite 策略      [ ]
    5.3 安全过滤优先      [ ]
    5.4 人类遥测          [ ]  received_frames=__ parse_failures=__
    5.5 Feature 2 看板    [ ]
    5.6 Engineer 路由     [ ]
    5.7 自动解说去重      [ ]
    5.8 WS 重连           [ ]
    5.9 Overlay 外观      [ ]
    5.10 TTS 播报         [ ]
```
