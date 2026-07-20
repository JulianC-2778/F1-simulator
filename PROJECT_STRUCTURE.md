# F1-simulator 项目结构

本仓库由 TORCS 1.3.7 引擎、Python AI 后端、AI Bot 和 Electron Overlay 组成。
TORCS 原始 C/C++、车辆、赛道和展示层不属于本轮后端重构范围。

## 正式运行架构

正式后端入口：

```bash
python3 -m midware.app
```

兼容入口（计划在 v2 后移除）：

```bash
python3 midware/commentary.py
```

```text
TORCS human driver
  -> UDP 3101
  -> TorcsUdpAdapter（唯一生产监听者）
  -> TelemetryService / TelemetryStore
       -> Commentary API / automatic loop
       -> Engineer API
       -> Coach API
       -> REST / WebSocket OutputBus

AI Bot
  <-> SCR UDP 3001
  -> POST /api/bot/status（非阻塞心跳）
  -> POST /api/bot/strategy（非阻塞低频策略）
  <- StrategyDecision
  -> 本地 safety_filter + 实时控制器

Commentary / Engineer / Coach / Bot strategy
  -> ModelBroker
  -> 有界优先级队列（默认并发 1）
  -> OpenAICompatibleGateway
  -> Granite / LM Studio
```

## 自研目录

```text
midware/
├── app.py                    # FastAPI app factory、正式 CLI、lifespan
├── commentary.py             # 23 行兼容启动包装器
├── runtime.py                # 迁移后的服务处理函数和后台循环装配
├── dependencies.py           # 共享服务引用
├── client.py                 # 旧调试工具使用的 REST client
├── api/                      # health/telemetry/commentary/engineer/coach/bot/config/ws 分组
├── schemas/                  # Pydantic 遥测、比赛、Bot、模型和输出协议
├── services/
│   ├── telemetry_service.py  # 共享 TelemetryStore 与 ingestor 生命周期
│   ├── feature_gate.py       # enabled/available/healthy/active
│   ├── model_broker.py       # 模型队列、优先级、过期和统计
│   └── bot_status_service.py # 服务端心跳年龄判断
├── adapters/
│   └── torcs_udp.py          # TORCS 字段适配和 UDP socket 所有权
├── shared/                   # 兼容共享组件
├── commentary_engine.py      # 解说事件检测
├── context_manager.py        # Prompt 与历史裁剪
├── feature2_core.py          # Coach 纯规则
└── static/                   # 后端提供的兼容网页

ai_bot.py                     # SCR 控制、本地安全规则、Broker client、心跳 reporter
chat_engineer.py              # Legacy Engineer API CLI client
chat_engineer_gui.py          # Legacy Engineer API GUI client
telemetry_analyzer.py         # Legacy Coach API CLI client
granite_client.py             # Deprecated direct-model compatibility module

tools/legacy/
├── telemetry_analyzer_legacy.py
└── feature2_service_legacy.py

tests/
├── fixtures/
└── unit/

overlay-app/                  # Electron 展示层，本轮未修改
src/ export/ data/ BUILD/     # TORCS vendored engine/content
```

## 核心运行约束

- 只有 `TelemetryService` 可在生产模式绑定 UDP 3101。
- 旧 Engineer/Coach 工具只调用主服务 API，不自动回退监听 UDP。
- standalone debug listener 必须显式启用并使用非 3101 端口。
- Feature 1/2/3 和 Bot 策略请求统一经过 `ModelBroker`。
- Bot 的转向、油门、制动和 `safety_filter()` 不等待模型或 Middleware。
- Feature enabled 状态真实控制 API 和自动 Commentary 循环，禁用返回 HTTP 409。
- WebSocket 保留旧 renderer 字段，同时增加 V1 `version/request_id/sequence`。
- `overlay-app/` 继续使用 `/ws` 和已有消息字段，无需同步修改。

## Feature 入口

| Feature | 正式接口 | 兼容工具 |
| --- | --- | --- |
| AI Race Engineer | `POST /api/engineer/ask` | `chat_engineer.py`、`chat_engineer_gui.py` |
| Telemetry Coach | `GET /api/coach/dashboard` | `telemetry_analyzer.py`、Feature 2 proxy |
| Procedural Commentary | 自动循环、`POST /api/commentary/manual` | `midware/commentary.py` launcher |
| AI Bot | `POST /api/bot/strategy`、`POST /api/bot/status` | `ai_bot.py --bot --granite` |

## 配置与端口

环境变量优先于 `config.json`，安全默认值最后。

| 用途 | 默认值 |
| --- | ---: |
| Middleware HTTP/WebSocket | `127.0.0.1:8880` |
| TORCS human telemetry UDP | `3101` |
| SCR Bot UDP | `3001` |
| Feature 2 compatibility proxy | `8766` |
| TTS | `8881` |

模型配置由 Middleware 的单一 `api_config` 提供给 Model Broker。API key 不写回仓库配置。

## 测试

```bash
python3 -m py_compile config.py telemetry_common.py car_state_source.py \
  granite_client.py chat_engineer.py chat_engineer_gui.py \
  telemetry_analyzer.py ai_bot.py overlay_broadcast.py \
  midware/*.py midware/api/*.py midware/shared/*.py \
  midware/schemas/*.py midware/services/*.py midware/adapters/*.py

python3 ai_bot.py
python3 test_a_module_latency.py
python3 -m unittest discover -s tests/unit -v
```

真实 TORCS/Granite、Electron 展示和完整运行矩阵属于 Phase 8 集成验收。
