# F1-simulator 项目结构总览

> 本项目 = **TORCS 赛车引擎（C/C++，第三方 vendored 代码）** + **AI 解说/工程师/遥测分析层（Python + Electron，团队自研）**。
> 下面的结构图只展开自研层的细节，TORCS 引擎部分做了折叠（体量巨大且基本未改动）。

## 1. 分层架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│  TORCS 游戏引擎（C/C++，vendored, 基本未修改）                        │
│  src/ export/ data/ doc/ BUILD/ scr_server/ robotgen/ ...            │
│  · human 驾驶员模块：每帧向 UDP 127.0.0.1:3101 推送遥测               │
│  · scr_server 驾驶员模块：UDP 3001，SCR 文本协议，供 AI 机器人接管驾驶 │
└───────┬─────────────────────────────────────────────┬─────────────────┘
        │ UDP :3101（遥测，只读旁路，不影响游戏本体）      │ UDP :3001（SCR，双向，直接控制车辆）
        ▼                                             ▼
┌───────────────────────────────────────┐   ┌───────────────────────────┐
│  遥测/解说后端（Python，自研）           │   │  ai_bot.py（Feature 4）    │
│  ★ 主链路见下方"4. 各功能链路详解"        │   │  独立进程，不经过 midware， │
│  ○ 早期原型/孤立模块见下方目录树           │   │  直接握手+收发 SCR 协议驾驶 │
└───────┬─────────────────────────────────┘   └───────────────────────────┘
        │ WebSocket ws://127.0.0.1:8880/ws  (host/port 来自 config.json，见下方 5 节)
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  展示层（Electron，自研，遵循 docs/display-layer-contract.md）        │
│  overlay-app/                                                        │
│    electron/main.js        — 多窗口管理（解说窗 + 工程师窗）          │
│    src/index.html + renderer.js            — 解说字幕/语音窗口        │
│    src/engineer.html + engineer-renderer.js — 工程师问答窗口          │
│    src/settings.html + settings.js          — 连接/语音设置           │
└─────────────────────────────────────────────────────────────────────┘

配套服务: tts_server.py (Kokoro TTS, :8881, 独立进程) · overlay_broadcast.py (外部进程→overlay 的轻量广播客户端)
文档: docs/*.md (解说循环、展示层契约、事件负载、Feature2独立服务、TTS配置)
```

> **✓ 端口不一致已修复（曾经的真实 bug，已通过 `config.py`/`config.json` 根治）**：
> `midware/commentary.py` 曾在 `c805cfc change port to 8880` 这次提交里把监听端口从 `8765` 改成 `8880`，
> 但 `docs/`、`README.md`、`midware/README.md`、`overlay-app/TESTING.md`、`midware/feature2_service.py`、
> `overlay_broadcast.py`、`chat_engineer.py`、`car_state_source.py` 等 9+ 处默认值/文档没有跟着改，
> 默认配置下会出现"Feature 2 看板拉不到遥测历史""`overlay_broadcast.py` 连不上 overlay"等问题。
> 现在所有端口/地址类默认值统一从仓库根目录的 `config.json` 读取（Python 侧通过 `config.py` 加载，
> Electron 侧 `overlay-app/electron/main.js` 直接读同一个文件），不再有任何文件各自写一份字面量默认值。
> 详见下方"5. `config.py` / `config.json`"一节。

## 2. 目录树（精简版）

```
F1-simulator/
├── src/ export/ data/ doc/ BUILD/ scr_server/ robotgen/     # TORCS 引擎源码 + 资源（vendored, C/C++）
│
├── midware/                    # ★ 当前主链路：解说/语音/看板后端
│   ├── telemetry.py            #   UDP 遥测采集 + 滑动窗口缓冲 + to_common_frame() 适配器（唯一的解析实现）
│   ├── commentary_engine.py    #   事件检测（优先级/冷却）
│   ├── context_manager.py      #   Prompt 组装 + 对话历史裁剪
│   ├── commentary.py           #   主服务：WebSocket/REST，广播中枢（端口来自 config.py）
│   ├── event_payload_config.py #   事件负载结构配置
│   ├── feature2_core.py        #   Feature2 看板规则引擎
│   ├── feature2_service.py     #   Feature2 独立 FastAPI 服务（端口来自 config.py）
│   └── static/                 #   feature2.html 等前端页面
│
├── overlay-app/                 # 展示层：Electron 双窗口 Overlay
│   ├── electron/main.js / preload.js   # main.js 读取 ../../config.json 派生 wsUrl 默认值
│   └── src/(index|engineer|settings).html + 对应 renderer.js
│
├── docs/                        # 架构契约文档（解说循环、展示层契约、事件负载参考等）
│
├── config.json                                # ★ 全项目端口/地址唯一数据源，Python 和 Electron 都读它
├── config.py                                  # ★ config.json 的 Python 加载器（env var > config.json > 硬编码兜底）
├── chat_engineer.py / chat_engineer_gui.py   # Feature 1: 工程师问答 CLI/GUI（默认探测 mainline REST，连不上再回退 UDP）
├── telemetry_analyzer.py                     # Feature 2 早期版本：驾驶建议（遥测采集已改用 midware.telemetry，探测 mainline 优先）
├── ai_bot.py                                 # Feature 4: SCR 赛车 AI 驾驶机器人
├── car_state_source.py                       # car_state 数据源（LiveCarStateSource 现也用 midware.telemetry 解析）
├── telemetry_common.py                       # 共享 AI 调用/摘要工具函数（遥测解析/缓冲部分已删除，统一到 midware/telemetry.py）
├── granite_client.py                         # LM Studio/Granite 客户端（Prompt 构建已合并进 midware/context_manager.py）
├── tts_server.py / overlay_broadcast.py      # TTS 服务 + 轻量广播客户端
│
├── voices/ kokoro-v1_0.pth                   # TTS 模型权重（已 gitignore，不入库）
└── PROJECT_STRUCTURE.md                      # 本文件
```

## 3. 关键事实速查

| 项 | 说明 |
|---|---|
| 引擎 vs 自研代码比例 | 仓库体量 8.0G 中绝大部分是 TORCS 引擎/资源/构建产物；自研 Python+JS 代码约 9000 行 |
| 当前生产链路 | `midware/*`（WebSocket+REST）→ `overlay-app`；`feature2_service.py` 读取 commentary 的 REST；端口全部来自 `config.json`，两者不会再失步 |
| 已知冗余（已处理） | Prompt 组装：`prompt_builder.py` 已删除，Feature 1 改用 `midware/context_manager.py` 的 `ContextManager`。遥测解析/缓冲：`telemetry_common.py` 的 `TelemetryBuffer`/`parse_telemetry()` 已删除，`race_commentator.py`（纯冗余）已删除，`telemetry_analyzer.py` 和 `car_state_source.py` 的 `LiveCarStateSource` 均已改用 `midware/telemetry.py` 的 `TelemetryStore`/`start_udp_listener`（camelCase→snake_case 通过新增的 `to_common_frame()` 适配，`feature2_core.py` 的同名重复函数也已删除并改为引用它）。端口/地址默认值：全部收敛到 `config.py`/`config.json`（见 5 节）。孤立代码：`middleware/`、`screenpipe/`、`fix_main.patch`、`test.wav` 已删除 |
| 已知冗余（未处理） | LM 调用客户端（`telemetry_common.chat_completion_text()` 非流式 vs `midware/commentary.py` 的 `call_ai()` 流式）；教练/Feature 2 的两套 prompt 实现（`telemetry_analyzer.py` AI 主导 vs `midware/feature2_core.py` 规则主导+AI补充，产品设计不同，需先决策） |
| 端口占用（已缓解） | `chat_engineer.py`/`chat_engineer_gui.py`/`telemetry_analyzer.py` 现在启动时都会先探测 mainline 的 REST API，能连上就走 HTTP 轮询、不再绑定 UDP；只有探测不到 mainline 时才回退绑定 UDP（端口号来自 `config.py` 的 `TELEMETRY_UDP_PORT`）。仍会冲突的场景：mainline 没启动、且这几个脚本同时手动跑（各自都会回退绑同一个端口） |
| 测试/CI | 仅 `lmstudio_smoke_test.py` 一个手动烟雾测试脚本，无 pytest 套件，无 GitHub Actions |
| 依赖管理 | `midware/requirements.txt`、`midware/requirements-feature2.txt` 存在，根目录的 Feature 脚本（`chat_engineer.py`/`chat_engineer_gui.py`/`telemetry_analyzer.py`/`ai_bot.py`）无独立 requirements 文件 |

## 4. 各功能链路详解

### 4.1 主链路 A — 自动实时解说（commentary，mainline）

```
TORCS(human) --UDP:3101--> midware/telemetry.py --滑动窗口(默认30s)-->
  midware/commentary_engine.py.detect_event() --按优先级+冷却过滤--> event payload -->
  midware/context_manager.py.format_event_prompt() + build_messages() --裁剪历史,控制token预算--> messages[] -->
  midware/commentary.py.call_ai() --流式 POST /v1/chat/completions--> LM Studio -->
  逐 token WebSocket 广播 {"type":"token"} --> overlay-app/src/renderer.js 缓冲 -->
  完成后广播 {"type":"ai_done","content":...} --> renderer.js 按标点切句 --> speechSynthesis 逐句播放
  （若 TTS 开启：commentary.py 额外调用 tts_server.py（端口来自 config.py）--> 广播 {"type":"tts_audio"} 二进制音频）
```

- 事件检测每 0.5s 跑一次，事件表见 `docs/event-payload-reference.md`（7 类事件，优先级 1–5，如 `contact`/`position_change`/`off_track`=5，`lap_complete`/`battle`=4，`pace_surge`=3，`pace_update`=1）。
- 高优先级事件可抢占正在生成的低优先级解说：`_commentary_task.cancel()` 中断流式请求，client 收到新 `ai_start` 后清空队列重新播放。
- 瓶颈在 LM Studio 推理延迟（8B 模型 15–30s，1–3B 模型 2–5s）。

### 4.2 主链路 B — Feature 2 遥测看板（standalone service）

```
midware/commentary.py 已缓存的遥测历史
  --HTTP GET /api/telemetry/history（地址来自 config.py 的 MIDWARE_BASE_URL）-->
  midware/feature2_service.py --> midware/feature2_core.py（规则引擎，生成驾驶建议/统计）-->
  midware/static/feature2.html （独立页面，端口来自 config.py 的 FEATURE2_PORT）
```

- 特意设计成不单独监听 UDP，而是复用主链路已采集的数据（`docs/feature2-standalone-service.md` 明确写了这个理由：避免和 mainline 抢 3101 端口）——这是全仓库里对"避免重复监听"处理得最好的一处，可作为其余脚本重构的参考模板。

### 4.3 Feature 1 工程师问答（engineer chat，遥测采集与展示已分别复用 mainline，Prompt 组装已合并）

```
car_state_source.py 的 choose_car_state_source() --优先探测--> HttpCarStateSource
  轮询 midware/commentary.py 的 GET /api/telemetry（地址来自 config.py，不绑 UDP） -- 探测不到才回退 -->
  LiveCarStateSource（内部改用 midware/telemetry.py 的 TelemetryStore + start_udp_listener，
  与 mainline 同一套解析代码，只是自己单独 bind UDP，端口同样来自 config.py）
  --> chat_engineer.py / chat_engineer_gui.py --> midware/context_manager.py 的 ContextManager
  （用 ENGINEER_PERSONA 人设初始化，与主链路共用同一套 token 预算裁剪逻辑）组装 prompt -->
  granite_client.py --POST /v1/chat/completions--> LM Studio --> 得到回答 -->
  overlay_broadcast.py --短连接 WS，tag source="engineer"--> midware/commentary.py 的 /ws relay -->
  overlay-app/src/engineer.html + engineer-renderer.js（仅渲染 source==="engineer" 的消息）
```

- `choose_car_state_source()` 现在默认"探测 mainline REST 优先，连不上才绑 UDP"，不再需要手动设置 `TORCS_ENGINEER_USE_MIDWARE_TELEMETRY`（该开关已删除）。`HttpCarStateSource`/`MIDWARE_BASE_URL` 默认值改为从 `config.py` 读取。
- `overlay_broadcast.py` 是唯一一个"外部短生命周期进程通过 WS 客户端方式接入 midware 广播"的实现（`docs/display-layer-contract.md` 推荐的标准接入方式），默认连接地址也已改为从 `config.py` 的 `MIDWARE_WS_URL` 读取。

### 4.4 Feature 2 早期版本（telemetry_analyzer.py，遥测采集已复用 mainline 解析代码）

```
telemetry_analyzer.py 的 MidwareBackedCollector --优先探测--> midware/commentary.py 的 REST（地址来自 config.py）
  能连上就 HTTP 轮询 /api/telemetry/history -- 连不上才回退 -->
  midware/telemetry.py 的 TelemetryStore + start_udp_listener（自己 bind UDP，端口来自 config.py）
  两种模式都经 midware/telemetry.py 的 to_common_frame() 转成 snake_case -->
  compact_track_profile()/compact_opponent_profile()（telemetry_common.py）
  --摘要遥测数据--> chat_completion_text() --直连 LM Studio--> 终端打印驾驶建议
```

- 纯 CLI 输出，不经过 `overlay-app`，不遵循 `docs/display-layer-contract.md`（该文档要求所有面向用户的 AI 输出都走 overlay 展示层）。
- 这条链路和 `midware/feature2_core.py`（4.2）的产品设计不同——这里是 AI 直接生成建议，`feature2_core.py` 是规则引擎主导、AI 只补一句说明——所以没有像 Feature 3（`race_commentator.py`，已删除）那样直接归档，保留下来但消除了遥测解析这部分的重复实现。
- Feature 3 的早期版本 `race_commentator.py` 纯粹是 `midware/commentary.py` 的功能子集（无事件优先级/冷却/WebSocket 广播/TTS），已直接删除。

### 4.5 独立系统 — Feature 4 AI 驾驶机器人（ai_bot.py）

```
TORCS scr_server 驾驶员模块 <--UDP:3001 双向 SCR 协议--> ai_bot.py:
  ScrClient 握手 --> parse_scr_state() 解析传感器字符串 -->
  compute_control()（策略参数化控制器：ATTACK/NORMAL/DEFEND/SAVE_FUEL/PIT）-->
  format_scr_control() 编码 --> UDP 回传车辆控制指令
```

- 与解说/看板/工程师问答完全独立的子系统：不产生 AI 文本、不经过 midware、不接入 overlay，只做"AI 直接开车"。是唯一一个不与其他链路共享代码或端口的模块，架构上最干净。

### 4.6 展示层内部路由（overlay-app）

```
electron/main.js 启动两个 BrowserWindow:
  窗口1 commentary (index.html+renderer.js, 底部居中) —— 显示 source 缺失/"commentary"/未知来源 的消息
  窗口2 engineer   (engineer.html+engineer-renderer.js, 顶部居中) —— 只显示 source==="engineer" 的消息
两窗口共用同一条 WebSocket 连接配置（settings.html+settings.js），各自独立重连、独立维护"Generating..."状态。
```

## 5. `config.py` / `config.json` —— 端口/地址的唯一数据源

**背景**：mainline 服务端口从 8765 改成 8880 之后，有 9+ 处文档和 Python 文件的默认值没有跟着改，是因为每个文件都各自写死了一份字面量默认值，没有共同的数据源。`config.json` + `config.py` 就是为了根治这一类问题。

```
config.json  （仓库根目录，唯一的设置存储文件）
  {
    "midware_host": "127.0.0.1",
    "midware_port": 8880,
    "telemetry_udp_port": 3101,
    "scr_udp_port": 3001,
    "feature2_port": 8766,
    "tts_port": 8881
  }
      │
      ├── Python 侧：config.py 读取 config.json，导出 MIDWARE_BASE_URL / MIDWARE_WS_URL /
      │   TELEMETRY_UDP_PORT / SCR_UDP_PORT / FEATURE2_BASE_URL / TTS_BASE_URL 等常量。
      │   优先级：环境变量 > config.json > 硬编码兜底值 —— 保留了每个功能原有的
      │   专属环境变量覆盖能力（如 TORCS_ENGINEER_MIDWARE_URL），只是"没设置环境变量时
      │   的默认值"从各自的字面量改成统一来自这一处。
      │   消费方：midware/commentary.py、midware/feature2_service.py、tts_server.py、
      │   chat_engineer.py、chat_engineer_gui.py、car_state_source.py、
      │   overlay_broadcast.py、telemetry_analyzer.py、ai_bot.py
      │
      └── Electron 侧：overlay-app/electron/main.js 用 Node 内置 fs/JSON 直接读同一个
          config.json（无需新增 npm 依赖），派生出 defaultSettings.connection.wsUrl，
          经既有的 settings:get IPC 传给 renderer.js / engineer-renderer.js / settings.js。
```

**只收录真正跨文件重复的设置**：`config.py` 里只放会被多处独立复制、容易失步的值（host/port 这一类）。像 `telemetry_analyzer.py` 里那些只有它自己用的调优参数（`LIVE_INTERVAL`、`TTS_VOICE` 等）不放进来——那些不存在"多处各写一份"的风险，硬塞进 config.py 只会让它变成大杂烩。

**已知残留**：`overlay-app/src/renderer.js`/`engineer-renderer.js`/`settings.js` 三个文件内部各自还留了一份 `wsUrl` 字面量默认值，作为"IPC `getSettings()` 请求返回前的瞬时占位符"（渲染进程受 `contextIsolation` 限制，不能直接读文件，只能等 IPC）。这个占位符几乎立刻会被 `main.js` 传回的真实值覆盖，实际不构成风险，但如果以后要做到完全没有任何字面量副本，需要给 `preload.js` 加一个同步的配置读取接口。
