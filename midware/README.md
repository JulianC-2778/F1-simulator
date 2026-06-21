# TORCS 比赛解说中间件

仿照 SillyTavern 上下文控制模式，为 TORCS 赛车模拟器提供实时 AI 比赛解说。

## 架构

```
midware/
├── server.py           # FastAPI 主服务（REST + WebSocket + UDP 监听）
├── context_manager.py  # SillyTavern 风格上下文窗口管理
├── requirements.txt
└── static/
    └── index.html      # Web UI
```

## 快速启动

```bash
cd midware
pip install -r requirements.txt
python server.py
# 打开浏览器 → http://localhost:8765
```

## 数据流

```
TORCS human 模块
   │  UDP :3101 (每采样帧推送 CSV 行)
   ▼
server.py (UDP 监听线程)
   │  latest_telemetry
   ▼
ContextManager.format_telemetry()   ← 字段过滤 + 自然语言描述
   │  user message
   ▼
ContextManager.build_messages()     ← Token 预算裁剪（SillyTavern 逻辑）
   │  [system, ...history, user]
   ▼
AI API (OpenAI / Anthropic / Ollama)
   │  streaming tokens
   ▼
WebSocket → 浏览器 UI
```

## 上下文控制（对应 SillyTavern 功能）

| SillyTavern 功能 | 本项目对应 |
|---|---|
| Context Template | `chat_template` 字段（ChatML / Instruct / Raw）|
| Context Size Limit | `max_context_tokens` 滑块 |
| Response Length | `max_response_tokens` 滑块 |
| Trim Strategy | `trim_strategy`（裁剪最旧 / 最新）|
| System Prompt | `commentator_persona`（解说员人设）|
| Keep in Context (pin) | `Message.pinned = True` |
| World Info / Author's Note | `format_telemetry()` 生成的遥测描述块 |

## API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web UI |
| GET | `/api/config` | 获取当前配置 |
| POST | `/api/config/api` | 更新 AI API 配置 |
| POST | `/api/config/context` | 更新上下文配置 |
| POST | `/api/commentary/manual` | 手动触发一次解说 |
| POST | `/api/commentary/clear` | 清除历史 |
| GET | `/api/telemetry` | 获取最新遥测数据 |
| POST | `/api/telemetry/push` | 手动注入遥测数据 |
| POST | `/api/csv/load` | 从 CSV 文件加载并解说 |
| WS | `/ws` | 实时推送 token 流 |

## TORCS 数据接入

**方式 1 — UDP 实时流（推荐）**

在启动 TORCS 前设置环境变量：
```bash
export TORCS_PLAYER_UDP_HOST=127.0.0.1
export TORCS_PLAYER_UDP_PORT=3101
./BUILD/bin/torcs
```
服务会自动监听 `:3101` 并在 UI 上实时显示遥测数据。

**方式 2 — CSV 文件**

比赛结束后，在 UI 的"数据源"面板填写 CSV 路径，点击"读取 CSV 并解说"。

**方式 3 — 手动演示**

点击"注入演示数据"后点击"▶ 解说"，无需运行 TORCS。
