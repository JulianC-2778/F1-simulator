# Commentary Loop Mechanism / 解说循环机制

---

## English

### Overview

The commentary system consists of four sequential stages: telemetry collection → event detection → AI generation → client playback. The loop runs continuously while the game is active.

```
TORCS Game → UDP → TelemetryStore → CommentaryEngine → LM Studio → WebSocket → Browser/Overlay
```

---

### Stage 1: Telemetry Collection

The TORCS human driver module sends a UDP packet to `127.0.0.1:3101` every game frame. A background thread in `telemetry.py` receives these packets and stores them in a sliding window buffer (default: 30 seconds of frames).

Each frame contains fields such as: `speed_x`, `race_pos`, `lap`, `damage`, `track_pos`, `fuel`, `cur_lap_time`, etc.

---

### Stage 2: Event Detection (every 0.5 s)

`_auto_commentary_loop` in `commentary.py` wakes up every 0.5 seconds and passes the most recent 6-second window of frames to `CommentaryEngine.next_decision()`.

`detect_event()` checks for the following candidate events, ranked by priority:

| Priority | Event | Trigger |
|---|---|---|
| 5 | `contact` | Damage delta ≥ 5 |
| 5 | `position_change` | Race position changed |
| 5 | `off_track` | `\|track_pos\|` > 1.0 |
| 4 | `lap_complete` | Lap counter incremented |
| 4 | `battle` | Front gap < 10 m at speed |
| 3 | `pace_surge` | Speed delta > 22 km/h with high throttle |
| 1 | `pace_update` | Interval timer (default 10 s) |

Before emitting, `_can_emit_event()` applies two cooldown checks:
- **Wall-clock cooldown**: 1 second between any two events
- **Signature cooldown**: per-event-type cooldown (1 s – 6 s) based on `EVENT_COOLDOWNS`

---

### Stage 3: Prompt Construction and AI Call

Once an event passes the cooldown checks, `generate_commentary()` in `commentary.py` is called as an async task:

1. `context_manager.format_event_prompt(payload)` — wraps the structured event data (position, lap, damage, track position, opponent gaps) into a natural-language instruction, injecting the commentator persona as the system prompt.
2. `ctx_mgr.build_messages()` — assembles the full message list: `[system: persona] + [trimmed history] + [current user message]`, respecting the token budget.
3. `call_ai()` — streams a POST request to LM Studio at `/v1/chat/completions`. Each token is broadcast immediately via WebSocket: `{"type": "token", "text": "..."}`.
4. On completion, broadcasts `{"type": "ai_done", "content": "<full text>"}`.

---

### Stage 4: Client Playback

The browser (`index2.html`) or Electron overlay handles the WebSocket messages:

| Message | Action |
|---|---|
| `ai_start` | `stopSpeech()`, clear queue, show loading indicator |
| `token` | Append to `pendingText` buffer |
| `ai_done` | Split `pendingText` into sentences by punctuation, enqueue, start playback |

Sentences are played sequentially via `speechSynthesis.speak()`. Each utterance's `onend` callback triggers the next sentence.

---

### Interruption: High-Priority Preemption

When a new event with priority ≥ current event arrives while commentary is still generating:

1. `_commentary_task.cancel()` — injects `CancelledError` into the streaming HTTP read, aborting the current AI request.
2. The new event's `generate_commentary()` starts immediately, broadcasting a fresh `ai_start`.
3. The client receives `ai_start`, calls `stopSpeech()`, and discards any queued sentences.
4. New tokens arrive and playback begins for the new event.

Low-priority events (e.g., `pace_update`) do **not** interrupt higher-priority ongoing commentary.

---

### Latency

The main bottleneck is LM Studio inference time. With Granite 4.1 8B, generation typically takes 15–30 seconds. Switching to a smaller model (1B–3B) reduces this to 2–5 seconds.

---
---

## 中文

### 概述

解说系统由四个顺序阶段组成：遥测采集 → 事件检测 → AI 生成 → 客户端播放。游戏运行期间循环持续运转。

```
TORCS游戏 → UDP → TelemetryStore → CommentaryEngine → LM Studio → WebSocket → 浏览器/Overlay
```

---

### 第一阶段：遥测数据采集

TORCS human 驾驶员模块每帧向 `127.0.0.1:3101` 发送一个 UDP 包。`telemetry.py` 中的后台线程持续接收这些数据包，并将其存入滑动窗口缓冲区（默认保留 30 秒的帧数据）。

每帧包含字段：`speed_x`（纵向车速）、`race_pos`（名次）、`lap`（圈数）、`damage`（损伤）、`track_pos`（赛道位置）、`fuel`（油量）、`cur_lap_time`（本圈用时）等。

---

### 第二阶段：事件检测（每 0.5 秒一次）

`commentary.py` 中的 `_auto_commentary_loop` 每 0.5 秒醒来一次，将最近 6 秒的帧数据传入 `CommentaryEngine.next_decision()`。

`detect_event()` 检查以下候选事件（按优先级排序）：

| 优先级 | 事件 | 触发条件 |
|---|---|---|
| 5 | `contact` 碰撞 | 损伤增量 ≥ 5 |
| 5 | `position_change` 名次变化 | 赛车名次改变 |
| 5 | `off_track` 出界 | `\|track_pos\|` > 1.0 |
| 4 | `lap_complete` 完圈 | 圈数计数器递增 |
| 4 | `battle` 近身缠斗 | 前车距离 < 10 m 且车速足够 |
| 3 | `pace_surge` 急加速 | 速度增量 > 22 km/h 且大油门 |
| 1 | `pace_update` 定时刷新 | 距上次解说超过 10 秒 |

事件通过后，`_can_emit_event()` 执行两项冷却检查：
- **墙钟冷却**：任意两次事件之间至少间隔 1 秒
- **签名冷却**：按事件类型各自冷却（1 秒 ~ 6 秒），由 `EVENT_COOLDOWNS` 控制

---

### 第三阶段：构建 Prompt 并调用 AI

事件通过冷却检查后，`commentary.py` 中的 `generate_commentary()` 作为异步任务被调用：

1. `context_manager.format_event_prompt(payload)` — 将结构化事件数据（名次、圈数、损伤、赛道位置、对手距离等）包装成自然语言指令，并注入解说员人设作为系统 prompt。
2. `ctx_mgr.build_messages()` — 组装完整消息列表：`[system: 人设] + [裁剪后的历史对话] + [当前 user 消息]`，控制在 token 预算之内。
3. `call_ai()` — 以流式方式向 LM Studio `/v1/chat/completions` 发送 POST 请求，每个 token 立即通过 WebSocket 广播：`{"type": "token", "text": "..."}`。
4. 生成完成后广播：`{"type": "ai_done", "content": "<完整文本>"}`。

---

### 第四阶段：客户端接收与播放

浏览器（`index2.html`）或 Electron Overlay 处理 WebSocket 消息：

| 消息类型 | 客户端动作 |
|---|---|
| `ai_start` | 调用 `stopSpeech()`，清空句子队列，显示加载指示 |
| `token` | 追加到 `pendingText` 缓冲区 |
| `ai_done` | 按标点切分 `pendingText` 为句子列表，入队，开始逐句播放 |

句子通过 `speechSynthesis.speak()` 逐句播放。每句 `onend` 回调触发下一句播放。

---

### 中断机制：高优先级事件抢占

当新事件优先级 ≥ 当前事件，且当前解说仍在生成时：

1. `_commentary_task.cancel()` — 向流式 HTTP 读取注入 `CancelledError`，中止当前 AI 请求。
2. 新事件的 `generate_commentary()` 立即启动，广播新的 `ai_start`。
3. 客户端收到 `ai_start`，调用 `stopSpeech()` 打断当前语音，清空队列。
4. 新 token 开始到来，播放新事件的解说。

低优先级事件（如 `pace_update` 定时刷新）**不会**打断优先级更高的进行中解说。

---

### 延迟分析

主要瓶颈在于 LM Studio 的推理速度。使用 Granite 4.1 8B 模型时，生成通常需要 15~30 秒。换用更小的模型（1B~3B）可将延迟降至 2~5 秒。
