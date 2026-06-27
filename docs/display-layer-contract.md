# TORCS AI Display Layer Contract

This document defines the standard display path for user-facing AI output in this project.

All new AI features should use this path:

```text
AI feature
  -> midware display broadcast
  -> WebSocket ws://127.0.0.1:8765/ws
  -> overlay-app
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

`overlay-app` owns:

- Caption display.
- Voice playback.
- Floating window behavior.
- Overlay connection settings.
- Model/API settings UI that talks to `midware`.
- User-facing HUD presentation.

`overlay-app` currently ships two floating windows, both connected to the same `ws://127.0.0.1:8765/ws` endpoint and the same shared connection/voice settings, but each only displaying messages routed to it (see "Multiple Overlay Windows" below):

| Window | Loads | Shows messages where |
| --- | --- | --- |
| Commentary overlay (bottom-center) | `overlay-app/src/index.html` + `renderer.js` | `source` is absent, `"commentary"`, or any value not recognized as a dedicated window |
| Engineer overlay (top-center) | `overlay-app/src/engineer.html` + `engineer-renderer.js` | `source === "engineer"` |

AI feature code owns:

- Collecting or analyzing data.
- Building prompts or structured payloads.
- Calling the model, directly or through shared midware helpers.
- Sending display messages through the standard WebSocket broadcast.

AI feature code should not create separate caption windows, browser toolbars, Tkinter popups, terminal-only presentation paths, or feature-specific display overlays for user-facing output.

This means a feature should not stand up its own ad-hoc window *outside* `overlay-app`. It does not mean every feature is stuck sharing one window: a feature that needs its own dedicated floating window should get one through `overlay-app`'s own officially supported mechanism -- a new `BrowserWindow` in `electron/main.js` plus a `source`-filtered renderer, exactly as described in "Multiple Overlay Windows (Source Routing)" below. The engineer overlay window (Feature 1) is a verified, working example of this: `overlay-app` now runs two floating windows side by side (commentary + engineer) from the same WebSocket stream.

## WebSocket Endpoint

The standard endpoint is:

```text
ws://127.0.0.1:8765/ws
```

`midware/commentary.py` currently exposes this endpoint and keeps track of connected clients.

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

Overlay behavior:

- Shows `Waiting for commentary...`.
- Does not speak.

### AI Start

Sent when an AI response begins.

```json
{
  "type": "ai_start"
}
```

Overlay behavior:

- Clears pending streamed text.
- Shows `Generating captions...`.
- Stops any currently playing voice.

### Token

Sent for streamed model output.

```json
{
  "type": "token",
  "text": "Brake late into "
}
```

Overlay behavior:

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

Overlay behavior:

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

Overlay behavior:

- Shows `Commentary error` plus a concise message.
- Does not speak.

## Existing Non-Display Messages

These messages may continue to be broadcast for dashboards, logs, or future UI, but the current overlay ignores them:

```json
{ "type": "telemetry_update" }
{ "type": "event_detected" }
{ "type": "user_msg" }
{ "type": "pong" }
```

Do not rely on these messages to show captions in `overlay-app`.

## Language Policy

The overlay caption HUD is English-first.

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

The overlay currently displays only `content`.

## Recommended Optional Fields

Future AI features may include these optional fields. The current overlay safely ignores unknown fields.

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

- `source`: feature identifier, such as `commentary`, `engineer`, `strategy`, or `incident_analysis`.
- `priority`: display priority, where higher values may later interrupt lower-priority messages.
- `stats`: token/context metadata for diagnostics.

`priority` and `stats` remain optional and are not yet used by the overlay. `source` is no longer purely advisory: as of the engineer overlay window, `overlay-app` uses it to route `ai_start`/`token`/`ai_done`/`error` to the correct window (see "Multiple Overlay Windows" below). Any feature that wants its own dedicated window must set `source` on every one of those four message types.

## Multiple Overlay Windows (Source Routing)

`overlay-app` now renders more than one floating window from the same WebSocket stream. Each window's renderer applies its own filter to `message.source`:

- `renderer.js` (commentary window): displays a message if `source` is missing, equals `"commentary"`, or is otherwise not claimed by a known dedicated window. This keeps existing features that never set `source` working unchanged.
- `engineer-renderer.js` (engineer window, Feature 1 / `overlay_broadcast.py`): displays a message only if `source === "engineer"`.

The `connected` message has no `source` and is shown by every window independently, so each window can report its own connection status.

Rules for new features:

- If your feature is fine sharing the existing commentary caption, omit `source` (or set it to `"commentary"`) and no overlay changes are needed.
- If your feature needs its own floating window (own position, own "Generating..." state, own voice playback that shouldn't be interrupted by commentary or vice versa), follow the pattern in `overlay_broadcast.py`:
  1. Pick a unique `source` string and tag every `ai_start`/`token`/`ai_done`/`error` message with it.
  2. Add a `electron/main.js` `BrowserWindow` (own bounds, non-overlapping with existing windows) and a corresponding `src/<feature>.html` + `src/<feature>-renderer.js` pair, cloned from `index.html`/`renderer.js`, that only reacts when `message.source === "<feature>"` (and still handles the sourceless `connected` message).
  3. Update the commentary window's filter in `renderer.js` if needed so it continues to ignore the new `source`.
  4. Document the new window in the table under "Display Ownership" above.

This is intentionally simple two-tier routing (per-source window ownership), not priority arbitration. `priority`-based interruption/merging across windows is still future work.

## Implementation Pattern

In `midware/commentary.py`, the existing path already follows this contract:

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

On the `midware` side, `/ws` in `commentary.py` makes this work by relaying: any text frame received from a connected client that is not the literal `"ping"` is parsed as JSON, and if its `type` is one of `ai_start`/`token`/`ai_done`/`error`, it is re-broadcast to every connected client via the same `broadcast()` used internally for commentary. This is what lets a short-lived external client like `overlay_broadcast.py` reach both overlay windows -- each window's own renderer still filters by `source` (see "Multiple Overlay Windows" above). Any other received text (unrecognized `type`, invalid JSON) is silently ignored.

## Testing Expectations

Any new feature using the display layer should verify:

- `ai_start` shows `Generating captions...`.
- streamed `token` messages do not create partial visible captions.
- `ai_done.content` appears in the overlay.
- voice playback occurs only on final `ai_done` text when enabled.
- `error.message` appears as a concise error state.
- telemetry and event messages do not disturb the current caption.

If your feature uses a dedicated `source` and window (see "Multiple Overlay Windows"), also verify:

- messages tagged with your `source` appear only in your window, not the commentary window.
- commentary messages (no `source`, or `source: "commentary"`) do not appear in your window.
- both windows reconnect and recover independently if `midware` restarts.

Use `overlay-app/TESTING.md` for the full overlay test flow.
