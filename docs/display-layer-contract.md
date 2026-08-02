# TORCS AI Display Layer Contract

This document defines the standard display path for user-facing AI output in this project.

All new AI features should use this path:

```text
AI feature
  -> midware display broadcast
  -> WebSocket ws://127.0.0.1:8880/ws
  -> midware dashboard (default) or overlay-app (only if the feature needs a dedicated floating caption window)
```

The goal is to keep captions, voice, connection settings, and race-HUD presentation consistent across the project.

## Scope

This contract applies to AI-generated output that a driver, presenter, or viewer should see or hear during a TORCS session, including:

- Live race commentary.
- Driving advice.
- Race engineer prompts.
- Incident analysis.
- Strategy alerts.
- Demo or classroom explanation text.

It does not apply to developer-only logs, debug traces, unit-test output, or backend health checks.

## Display Ownership

`midware/static/dashboard.html` is the default, in-browser display surface. It owns:

- Race commentary captions, voice playback, and the commentary/engineer/coach/bot control tabs.
- Configuration for every feature that broadcasts over `/ws` and doesn't request a dedicated overlay window.

`overlay-app` is an Electron floating-window app reserved for features that specifically need an always-on-top caption HUD outside the browser. Today that is **Feature 1 (AI Racing Engineer Chatbot) only** -- it owns:

- The engineer caption window (`overlay-app/src/engineer.html` + `engineer-renderer.js`), which shows only messages tagged `"source": "engineer"`.
- Voice playback, floating window behavior, and connection settings for that window.
- A shared settings window (Model API, Voice, Server TTS, overlay connection) reachable from the engineer window's app menu.

Race commentary (Feature 3) does **not** use `overlay-app`. It used to ship its own floating caption window (`overlay-app/src/index.html` + `renderer.js`); that window was removed once the dashboard grew its own "Commentary feed" tab, and `midware`'s `/ws` broadcast for untagged/`"commentary"`-sourced messages is now consumed by the dashboard instead.

AI feature code owns:

- Collecting or analyzing data.
- Building prompts or structured payloads.
- Calling the model, directly or through shared midware helpers.
- Sending display messages through the standard WebSocket broadcast.

AI feature code should not create separate caption windows, browser toolbars, Tkinter popups, terminal-only presentation paths, or feature-specific display overlays for user-facing output. New features default to showing up in the dashboard automatically (any message not tagged with a dedicated `source` that already owns its own overlay window). A feature should only add its own `overlay-app` floating window if it specifically needs an always-on-top HUD outside the browser -- that requires a new `BrowserWindow` in `electron/main.js` plus a `source`-filtered renderer, following the pattern the engineer window already establishes.

## WebSocket Endpoint

The standard endpoint is:

```text
ws://127.0.0.1:8880/ws
```

`midware/runtime.py` (the FastAPI app started via `python3 -m midware.app`) exposes this endpoint and keeps track of connected clients. `midware/commentary.py` is a deprecated one-cycle compatibility wrapper kept only for older scripts/docs -- do not treat it as the primary entry point.

## Required Message Types

### Connected

Sent by the backend when a client connects.

```json
{
  "type": "connected",
  "stats": {},
  "has_telemetry": true
}
```

Dashboard/overlay behavior:

- Shows a waiting/idle state (`Waiting for commentary...` on the dashboard's commentary tab, `Waiting for engineer reply...` on the engineer overlay).
- Does not speak.

### AI Start

Sent when an AI response begins.

```json
{
  "type": "ai_start"
}
```

Behavior:

- Clears pending streamed text.
- Shows a "generating" state.
- Stops any currently playing voice.

### Token

Sent for streamed model output.

```json
{
  "type": "token",
  "text": "Brake late into "
}
```

Behavior:

- Buffers the token text.
- Does not update the visible caption yet.
- Does not speak.

### AI Done

Sent when the AI response is complete.

```json
{
  "type": "ai_done",
  "content": "Brake late into turn one, then ease back onto the throttle.",
  "stats": {}
}
```

Behavior:

- Displays `content` if present.
- Falls back to buffered `token` text if `content` is empty.
- Speaks the final text if voice is enabled.

### Error

Sent when a user-facing AI action fails.

```json
{
  "type": "error",
  "message": "API 500: model unavailable"
}
```

Behavior:

- Shows an error state plus a concise message (`Commentary error` on the dashboard, `Engineer error` on the engineer overlay).
- Does not speak.

## Existing Non-Display Messages

These messages may continue to be broadcast for dashboards, logs, or future UI, but `overlay-app` ignores them:

```json
{ "type": "telemetry_update" }
{ "type": "event_detected" }
{ "type": "user_msg" }
{ "type": "pong" }
```

Do not rely on these messages to show captions in `overlay-app`. The dashboard does consume `telemetry_update`/`event_detected` for its own panels.

## Language Policy

The engineer overlay caption HUD is English-first.

For content that should appear in the overlay, prefer final English text in:

```json
{
  "type": "ai_done",
  "content": "Final English caption."
}
```

If a feature needs bilingual or structured output later, add explicit fields while preserving `content` as the display-safe English caption:

```json
{
  "type": "ai_done",
  "content": "Final English caption.",
  "content_zh": "中文解说。",
  "source": "commentary"
}
```

The engineer overlay currently displays only `content`.

## Recommended Optional Fields

Future AI features may include these optional fields. The dashboard and overlay safely ignore unknown fields.

```json
{
  "type": "ai_done",
  "source": "commentary",
  "priority": 2,
  "content": "Final English caption.",
  "stats": {}
}
```

Suggested meaning:

- `source`: feature identifier, such as `commentary`, `engineer`, `coach`, `bot`, `strategy`, or `incident_analysis`.
- `priority`: display priority, where higher values may later interrupt lower-priority messages.
- `stats`: token/context metadata for diagnostics.

`priority` and `stats` remain optional and are not yet used by the dashboard or overlay. `source` is not purely advisory: `overlay-app`'s single engineer window only renders `source === "engineer"`; everything else is left for the dashboard to render.

## Adding a Dedicated Overlay Window

`overlay-app` renders exactly one floating window today (the engineer window). A feature should only add a second one if it specifically needs its own always-on-top HUD outside the browser (own position, own "Generating..." state, own voice playback that shouldn't be interrupted by the engineer window or vice versa) -- most new features should just rely on the dashboard instead. To add one, follow the pattern `overlay_broadcast.py` / `engineer-renderer.js` already establish:

1. Pick a unique `source` string and tag every `ai_start`/`token`/`ai_done`/`error` message with it.
2. Add an `electron/main.js` `BrowserWindow` (own bounds, non-overlapping with the engineer window) and a corresponding `src/<feature>.html` + `src/<feature>-renderer.js` pair, cloned from `engineer.html`/`engineer-renderer.js`, that only reacts when `message.source === "<feature>"` (and still handles the sourceless `connected` message).
3. Document the new window in the table under "Display Ownership" above.

This is intentionally simple two-tier routing (per-source window ownership), not priority arbitration. `priority`-based interruption/merging across windows is still future work.

## Implementation Pattern

In `midware/runtime.py`, the existing path already follows this contract:

```python
await broadcast({"type": "ai_start"})
await broadcast({"type": "token", "text": token})
await broadcast({"type": "ai_done", "content": reply, "stats": ctx_mgr.stats()})
await broadcast({"type": "error", "message": str(e)})
```

New AI features should use the same message types. If a feature runs outside `midware`, route its display output back through `midware` instead of opening a separate UI.

For a feature that runs as its own Python process outside `midware` (no shared event loop to call `broadcast()` from), connect to `midware` as a normal external WebSocket client instead. Feature 1's `overlay_broadcast.py` (repo root) is the reference implementation: it opens a short-lived connection per call, sends one tagged message, and closes -- and it never raises or blocks the caller if `midware`/`overlay-app` are not running:

```python
import overlay_broadcast

overlay_broadcast.broadcast_engineer_start()
# ... call the model ...
overlay_broadcast.broadcast_engineer_reply(answer)   # or broadcast_engineer_error(str(exc))
```

On the `midware` side, `/ws` makes this work by relaying: any text frame received from a connected client that is not the literal `"ping"` is parsed as JSON, and if its `type` is one of `ai_start`/`token`/`ai_done`/`error`, it is re-broadcast to every connected client via the same `broadcast()` used internally for commentary. This is what lets a short-lived external client like `overlay_broadcast.py` reach the engineer overlay window -- the dashboard and the engineer window each still filter by `source` on their own. Any other received text (unrecognized `type`, invalid JSON) is silently ignored.

## Testing Expectations

Any new feature using the display layer should verify:

- `ai_start` shows a "generating" state wherever the feature displays.
- streamed `token` messages do not create partial visible captions.
- `ai_done.content` appears in the display surface.
- voice playback occurs only on final `ai_done` text when enabled.
- `error.message` appears as a concise error state.
- telemetry and event messages do not disturb the current caption in `overlay-app`.

If your feature uses a dedicated `source` and its own overlay window, also verify:

- messages tagged with your `source` appear only in your window, not the engineer window or the dashboard.
- messages tagged with a different `source` (or no `source`) do not appear in your window.
- your window and the engineer window reconnect and recover independently if `midware` restarts.

Use `overlay-app/TESTING.md` for the full overlay test flow.
