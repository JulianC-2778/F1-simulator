# TORCS Unified Integration Contract

This document defines the low-friction contract for merging the four AI
features without blocking parallel feature work. Existing scripts may keep
their current entry points during the transition, but new UI and orchestration
work should target the APIs and message shapes below.

## Runtime Shape

The project uses one shared Middleware runtime with optional feature modules:

- `commentary`: live race commentary.
- `engineer`: driver question answering.
- `coach`: telemetry analysis and driving guidance.
- `bot`: AI driver status and strategy monitoring.

The official entrypoint is `python3 -m midware.app`. `midware/commentary.py`
is a one-cycle compatibility launcher.

## Feature Discovery

Dashboard and launcher UI should use these endpoints:

```text
GET /api/features
GET /api/features/status
POST /api/features/enabled
```

`/api/features` returns feature metadata:

```json
{
  "features": [
    {
      "name": "commentary",
      "label": "Live Commentary",
      "description": "Real-time event detection and commentary.",
      "available": true,
      "entrypoint": "python3 -m midware.app"
    }
  ]
}
```

`/api/features/status` returns runtime state:

```json
{
  "features": [
    {
      "name": "commentary",
      "enabled": true,
      "available": true,
      "healthy": true,
      "active": true,
      "last_error": "",
      "last_update": 0,
      "details": {}
    }
  ]
}
```

Feature settings gate real handlers and background work. Disabled feature APIs
return HTTP 409; the setting does not pretend to manage external processes.

`POST /api/features/enabled` records the desired feature combination without
force-starting or force-stopping legacy scripts:

```json
{
  "enabled": ["commentary", "engineer", "coach"]
}
```

This is the transition path for supporting any 2-feature, 3-feature, or
4-feature combination while avoiding disruptive process orchestration changes.

## Health And Race Snapshot

Unified UI and smoke tests should use:

```text
GET /api/health
GET /api/race/snapshot
```

`/api/race/snapshot` returns the canonical compact race state that every feature
should prefer over local one-off summaries:

```json
{
  "snapshot": {
    "available": true,
    "session_id": 1,
    "sim_time": 123.4,
    "updated_at": 0,
    "car": {
      "lap": 2,
      "race_pos": 3,
      "speed": 145.2,
      "track_pos": 0.2,
      "damage": 0,
      "fuel": 18.2
    },
    "rankings": []
  }
}
```

`/api/health` returns midware, telemetry, model scheduler, TTS, overlay, feature,
and combination status in one response.

## Unified WebSocket Message

All user-facing feature output should be normalized to this envelope:

```json
{
  "type": "message",
  "version": 1,
  "source": "engineer",
  "request_id": "uuid",
  "sequence": 0,
  "level": "info",
  "title": "",
  "content": "Brake earlier before turn-in.",
  "payload": {},
  "timestamp": 0
}
```

Compatibility rule: legacy messages such as `ai_start`, `token`, `ai_done`,
`error`, `telemetry_update`, and `event_detected` remain valid. New code should
include `source` whenever possible.

Recommended sources:

- `commentary`
- `engineer`
- `coach`
- `bot`
- `system`

Recommended levels:

- `info`
- `success`
- `warn`
- `error`

## Shared Data APIs

Telemetry should be read from the shared midware store whenever possible:

```text
GET /api/telemetry
GET /api/telemetry/history?seconds=10
```

Coach / Feature 2 should move toward the main service API:

```text
GET /api/coach/dashboard
```

The standalone Feature 2 service may remain temporarily as a compatibility
entry point.

Engineer should move toward the main service API:

```text
POST /api/engineer/ask
GET  /api/engineer/history
POST /api/engineer/clear
```

Bot should remain a separate realtime control process, but report status to:

```text
GET  /api/bot/status
POST /api/bot/status
POST /api/bot/strategy
```

Use `tools/runtime_matrix_check.py` to validate all 2-feature, 3-feature, and
4-feature combinations against the shared runtime APIs.

## Model Gateway Direction

All production feature model calls route through `ModelBroker`:

```python
await model.chat(task="commentary", messages=messages)
await model.chat(task="engineer", messages=messages)
await model.json(task="coach", messages=messages)
```

All production model requests go through `midware/services/model_broker.py`.
The default policy serializes local model calls and uses these priorities:

- `engineer`: highest user-facing priority.
- `bot`: high priority but should keep short timeouts.
- `commentary_event`: medium priority.
- `commentary`: normal priority.
- `coach`: low priority.
- `commentary_baseline`: lowest priority.

## Parallel Development Rules

- Feature modules must not import each other directly.
- Shared behavior belongs in `midware/shared` or an agreed shared module.
- Legacy scripts may call the shared service over HTTP instead of binding UDP.
- The web dashboard should depend on this document, not on individual scripts.
- Keep old environment variables working until the team agrees to remove them.
