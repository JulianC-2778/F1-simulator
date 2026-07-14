# TORCS Feature Unification Migration Plan

This plan keeps existing feature entry points usable while gradually moving the
project toward one shared runtime and one unified web UI.

## Goals

- Keep all current feature scripts runnable during the transition.
- Add stable shared APIs for the unified web team.
- Move duplicate telemetry, model, prompt, status, and output logic into shared
  modules one piece at a time.
- Support any 2-feature, 3-feature, or 4-feature runtime combination.

## Current Shared APIs

The main midware service now exposes:

```text
GET  /api/health
GET  /api/features
GET  /api/features/status
POST /api/features/enabled
GET  /api/race/snapshot
GET  /api/coach/dashboard
POST /api/engineer/ask
GET  /api/engineer/history
POST /api/engineer/clear
GET  /api/bot/status
POST /api/bot/status
```

## Feature Migration Map

### Commentary

Current main entry point:

```text
midware/commentary.py
```

Migration status:

- Already acts as the main shared service.
- Uses `midware/shared/model_gateway.py` for model calls.
- Uses `midware/shared/model_scheduler.py` to avoid overlapping local model
  requests.
- Exposes feature status, health, coach dashboard, engineer API, bot status,
  and race snapshot APIs.

Next steps:

- Move route groups into smaller modules once team activity around
  `commentary.py` slows down.
- Add event-topic subscriptions for the unified web UI if the single `/ws`
  channel becomes too noisy.

### Engineer

Current legacy entry points:

```text
chat_engineer.py
chat_engineer_gui.py
```

New shared API:

```text
POST /api/engineer/ask
GET  /api/engineer/history
POST /api/engineer/clear
```

Migration rule:

- Keep CLI and GUI working.
- New web UI should use `/api/engineer/ask`.
- Later, CLI and GUI can become thin clients over the shared API.

Recommended request:

```json
{
  "question": "Should I pit now?"
}
```

Optional advanced request:

```json
{
  "question": "How are the tyres?",
  "car_state": {
    "speed": 120,
    "rpm": 7200,
    "gear": 4,
    "track_pos": 0.2,
    "damage": 0,
    "fuel": 18.2,
    "lap_time": 42.1,
    "problems": []
  }
}
```

### Coach / Feature 2

Current legacy entry point:

```text
midware/feature2_service.py
```

New shared API:

```text
GET /api/coach/dashboard
```

Migration rule:

- Keep the standalone service for existing Feature 2 development.
- Unified web UI should prefer `/api/coach/dashboard` from the main midware
  service.
- Later, `feature2_service.py` can become a compatibility wrapper or be marked
  legacy.

### Bot

Current entry point:

```text
ai_bot.py
```

New shared status API:

```text
GET  /api/bot/status
POST /api/bot/status
```

Migration rule:

- Do not move the realtime SCR control loop into the main service.
- Bot remains an independent process.
- Bot should report status asynchronously to midware.
- Midware and the unified web UI display bot state but do not block driving.

Recommended status payload:

```json
{
  "connected": true,
  "strategy": "NORMAL",
  "speed": 120,
  "gear": 4,
  "last_control": {
    "accel": 0.7,
    "brake": 0,
    "steer": 0.12
  },
  "fallback": false,
  "error": ""
}
```

## Shared Runtime Rules

- Only the main midware service should own the primary telemetry UDP listener
  during integrated runs.
- Other features should read `/api/race/snapshot`, `/api/telemetry`, or
  `/api/telemetry/history`.
- Feature modules should not import each other directly.
- User-facing messages should include `source`:
  `commentary`, `engineer`, `coach`, `bot`, or `system`.
- Model calls should migrate toward `midware/shared/model_scheduler.py`.
- Old environment variables should remain supported until the team agrees to
  remove them.

## Combination Testing

Use:

```bash
python tools/runtime_matrix_check.py --base-url http://127.0.0.1:8880
```

This checks the shared APIs and posts all 2-feature, 3-feature, and 4-feature
combinations to `/api/features/enabled`.

The script does not start or stop feature processes; it only validates the
shared runtime contract.
