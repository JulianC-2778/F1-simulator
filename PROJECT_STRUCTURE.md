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
        │ WebSocket ws://127.0.0.1:8880/ws  (⚠ 见下方"端口不一致"提示)
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

> **⚠ 端口不一致（当前代码里的真实 bug，非历史遗留）**：`midware/commentary.py` 在
> `c805cfc change port to 8880` 这次提交里把监听端口从 `8765` 改成了 `8880`，
> `overlay-app` 的三个前端文件（`renderer.js`/`engineer-renderer.js`/`settings.js`）也同步改成了 `8880`，
> 两者是一致的、能跑通的。**但**以下文件的默认值还停留在旧的 `8765`，没有跟着改：
> `docs/display-layer-contract.md`、`docs/feature2-standalone-service.md`、`README.md`、`midware/README.md`、
> `overlay-app/TESTING.md`、`midware/feature2_service.py`（`COMMENTARY_BASE_URL` 默认值）、
> `chat_engineer.py`（`MIDWARE_BASE_URL` 默认值）、`overlay_broadcast.py`（`OVERLAY_WS_URL` 默认值）、
> `car_state_source.py`（默认 `base_url`）。
> 实际影响：不设置对应环境变量（`TORCS_FEATURE2_COMMENTARY_URL` / `TORCS_ENGINEER_MIDWARE_URL` /
> `TORCS_ENGINEER_OVERLAY_WS_URL`）时，**Feature 2 看板拉取不到遥测历史、Feature 1 工程师问答的语音广播连不上 overlay**，
> 因为它们默认去连 8765，而服务实际跑在 8880。这是全仓库唯一一处"代码已改、文档和多处默认值未同步"的具体断点，建议优先修。

## 2. 目录树（精简版）

```
F1-simulator/
├── src/ export/ data/ doc/ BUILD/ scr_server/ robotgen/     # TORCS 引擎源码 + 资源（vendored, C/C++）
│
├── midware/                    # ★ 当前主链路：解说/语音/看板后端
│   ├── telemetry.py            #   UDP 遥测采集 + 滑动窗口缓冲
│   ├── commentary_engine.py    #   事件检测（优先级/冷却）
│   ├── context_manager.py      #   Prompt 组装 + 对话历史裁剪
│   ├── commentary.py           #   主服务：WebSocket/REST，广播中枢 (:8880，见上方端口提示)
│   ├── event_payload_config.py #   事件负载结构配置
│   ├── feature2_core.py        #   Feature2 看板规则引擎
│   ├── feature2_service.py     #   Feature2 独立 FastAPI 服务 (:8766)
│   └── static/                 #   feature2.html 等前端页面
│
├── middleware/                  # ○ 旧版独立数据中间层（README 标注 Legacy，当前不被依赖）
│   ├── main.py / parser.py / cache.py
│
├── overlay-app/                 # 展示层：Electron 双窗口 Overlay
│   ├── electron/main.js / preload.js
│   └── src/(index|engineer|settings).html + 对应 renderer.js
│
├── screenpipe/                  # ○ 孤立模块：旧 torcs-1.3.7 补丁截屏/IPC 原型，未被引用
│
├── docs/                        # 架构契约文档（解说循环、展示层契约、事件负载参考等）
│
├── chat_engineer.py / chat_engineer_gui.py   # ○ Feature 1: 工程师问答 CLI/GUI（独立于 midware）
├── telemetry_analyzer.py                     # ○ Feature 2 早期版本：驾驶建议
├── race_commentator.py                       # ○ Feature 3 早期版本：程序化解说
├── ai_bot.py                                 # ○ Feature 4: SCR 赛车 AI 驾驶机器人
├── telemetry_common.py / car_state_source.py # 上述脚本共享的遥测/AI调用工具函数
├── granite_client.py / prompt_builder.py     # LM Studio/Granite 客户端 + Prompt 构建
├── tts_server.py / overlay_broadcast.py      # TTS 服务 + 轻量广播客户端
│
├── voices/ kokoro-v1_0.pth                   # TTS 模型权重（已 gitignore，不入库）
└── PROJECT_STRUCTURE.md                      # 本文件
```

## 3. 关键事实速查

| 项 | 说明 |
|---|---|
| 引擎 vs 自研代码比例 | 仓库体量 8.0G 中绝大部分是 TORCS 引擎/资源/构建产物；自研 Python+JS 代码约 9000 行 |
| 当前生产链路 | `midware/*`（WebSocket+REST 实际监听 :8880）→ `overlay-app`；`feature2_service.py`（:8766）应读取 :8880 的 REST，但默认值仍写 :8765（见上方端口不一致提示） |
| 已知冗余 | `middleware/`（Legacy）、根目录 4 个 Feature 脚本与 `midware/` 存在同类逻辑重复（端口、模型调用、遥测缓冲） |
| 孤立模块 | `screenpipe/`（未被任何当前代码引用） |
| 端口不一致 | `midware/commentary.py` 实际监听 :8880，但 9+ 处文档 和 3 个 Python 默认值仍写 :8765，默认配置下会连接失败 |
| 端口占用 | 至少 6 个文件默认监听 UDP :3101（`midware/telemetry.py`、`middleware/main.py`、`chat_engineer.py`、`chat_engineer_gui.py`、`race_commentator.py`、`telemetry_analyzer.py`、`car_state_source.py`），同时运行会冲突 |
| 测试/CI | 仅 `lmstudio_smoke_test.py` 一个手动烟雾测试脚本，无 pytest 套件，无 GitHub Actions |
| 依赖管理 | `midware/requirements.txt`、`midware/requirements-feature2.txt`、`middleware/requirements.txt` 存在，根目录 4 个 Feature 脚本无独立 requirements 文件 |

## 4. 各功能链路详解

### 4.1 主链路 A — 自动实时解说（commentary，mainline）

```
TORCS(human) --UDP:3101--> midware/telemetry.py --滑动窗口(默认30s)-->
  midware/commentary_engine.py.detect_event() --按优先级+冷却过滤--> event payload -->
  midware/context_manager.py.format_event_prompt() + build_messages() --裁剪历史,控制token预算--> messages[] -->
  midware/commentary.py.call_ai() --流式 POST /v1/chat/completions--> LM Studio -->
  逐 token WebSocket 广播 {"type":"token"} --> overlay-app/src/renderer.js 缓冲 -->
  完成后广播 {"type":"ai_done","content":...} --> renderer.js 按标点切句 --> speechSynthesis 逐句播放
  （若 TTS 开启：commentary.py 额外调用 tts_server.py :8881 --> 广播 {"type":"tts_audio"} 二进制音频）
```

- 事件检测每 0.5s 跑一次，事件表见 `docs/event-payload-reference.md`（7 类事件，优先级 1–5，如 `contact`/`position_change`/`off_track`=5，`lap_complete`/`battle`=4，`pace_surge`=3，`pace_update`=1）。
- 高优先级事件可抢占正在生成的低优先级解说：`_commentary_task.cancel()` 中断流式请求，client 收到新 `ai_start` 后清空队列重新播放。
- 瓶颈在 LM Studio 推理延迟（8B 模型 15–30s，1–3B 模型 2–5s）。

### 4.2 主链路 B — Feature 2 遥测看板（standalone service）

```
midware/commentary.py 已缓存的遥测历史
  --HTTP GET /api/telemetry/history (实际:8880，feature2_service.py 默认值写的是:8765) -->
  midware/feature2_service.py --> midware/feature2_core.py（规则引擎，生成驾驶建议/统计）-->
  midware/static/feature2.html （独立页面，端口 :8766，http://127.0.0.1:8766/feature2）
```

- 特意设计成不单独监听 UDP，而是复用主链路已采集的数据（`docs/feature2-standalone-service.md` 明确写了这个理由：避免和 mainline 抢 3101 端口）——这是全仓库里对"避免重复监听"处理得最好的一处，可作为其余脚本重构的参考模板。

### 4.3 早期原型 — Feature 1 工程师问答（engineer chat，独立于 midware）

```
car_state_source.py --两种模式--:
  (a) 自己监听 UDP:3101 (与 mainline 冲突)
  (b) TORCS_ENGINEER_USE_MIDWARE_TELEMETRY=true 时改为 HTTP 轮询 midware 的 REST（同样受 8765/8880 端口不一致影响）
  --> chat_engineer.py / chat_engineer_gui.py --> prompt_builder.py 组装工程师人设 prompt -->
  granite_client.py --POST /v1/chat/completions--> LM Studio --> 得到回答 -->
  overlay_broadcast.py --短连接 WS，tag source="engineer"--> midware/commentary.py 的 /ws relay -->
  overlay-app/src/engineer.html + engineer-renderer.js（仅渲染 source==="engineer" 的消息）
```

- `overlay_broadcast.py` 是唯一一个"外部短生命周期进程通过 WS 客户端方式接入 midware 广播"的实现（`docs/display-layer-contract.md` 推荐的标准接入方式），设计是对的，但默认连接地址仍是旧端口 8765。

### 4.4 早期原型 — Feature 2/3 早期版本（telemetry_analyzer.py / race_commentator.py）

```
telemetry_common.py.TelemetryBuffer --自己监听 UDP:3101--> compact_track_profile()/compact_opponent_profile()
  --摘要遥测数据--> chat_completion_text() --直连 LM Studio--> 终端打印驾驶建议 / 程序化解说文本
```

- 纯 CLI 输出，不经过 `overlay-app`，不遵循 `docs/display-layer-contract.md`（该文档要求所有面向用户的 AI 输出都走 overlay 展示层）。功能已被 `midware/` 主链路（4.1/4.2）覆盖，是判断"可归档"的主要候选。

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
