#!/usr/bin/env python3
"""
overlay_broadcast.py — best-effort bridge from Feature 1 (AI Racing Engineer
Chatbot) into the shared overlay display layer.

Per docs/display-layer-contract.md, AI feature code should not build its own
caption window -- it should send display events through midware's WebSocket
broadcast (ws://127.0.0.1:8765/ws) and let the existing overlay-app render
them. This module tags every message with "source": "engineer" so the
overlay can route it to the dedicated engineer caption window
(overlay-app/src/engineer.html) instead of the race-commentary one.

Design goals:
  - Never raise. Never block the caller for more than ~1-2 seconds.
  - If midware/commentary.py is not running (most common case while just
    testing chat_engineer.py / chat_engineer_gui.py on their own), every
    call here is a silent no-op. The chatbot must work exactly the same
    with or without midware/overlay-app running.
  - No hard dependency: if the optional `websocket-client` package is not
    installed, this module degrades to a no-op instead of crashing import.

Env vars:
    TORCS_ENGINEER_OVERLAY_BROADCAST   - "false" to disable entirely (default: true)
    TORCS_ENGINEER_OVERLAY_WS_URL      - override the midware WebSocket URL
                                          (default: ws://127.0.0.1:8765/ws)
"""

from __future__ import annotations

import json
import os

try:
    import websocket  # "websocket-client" package (optional dependency)
except ImportError:  # pragma: no cover - optional dependency may be missing
    websocket = None  # type: ignore[assignment]


OVERLAY_WS_URL = os.getenv("TORCS_ENGINEER_OVERLAY_WS_URL", "ws://127.0.0.1:8765/ws")
OVERLAY_SOURCE = "engineer"
CONNECT_TIMEOUT_SECONDS = 1.5


def _overlay_enabled() -> bool:
    return os.getenv("TORCS_ENGINEER_OVERLAY_BROADCAST", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _send_messages(messages: list[dict]) -> None:
    """Open a short-lived connection, send all messages in order, close.

    Any failure (midware not running, wrong port, network hiccup, missing
    dependency, ...) is swallowed on purpose -- this is a "nice to have"
    side channel, never a requirement for chat_engineer.py /
    chat_engineer_gui.py to keep working.
    """
    if not _overlay_enabled() or websocket is None:
        return
    try:
        connection = websocket.create_connection(OVERLAY_WS_URL, timeout=CONNECT_TIMEOUT_SECONDS)
    except Exception:
        return
    try:
        for message in messages:
            connection.send(json.dumps(message))
    except Exception:
        pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


def broadcast_engineer_start() -> None:
    """Tell the shared overlay that an engineer reply is being generated."""
    _send_messages([{"type": "ai_start", "source": OVERLAY_SOURCE}])


def broadcast_engineer_reply(content: str) -> None:
    """Tell the shared overlay the final engineer reply text to display."""
    _send_messages([{"type": "ai_done", "source": OVERLAY_SOURCE, "content": content}])


def broadcast_engineer_error(message: str) -> None:
    """Tell the shared overlay that the last engineer call failed."""
    _send_messages([{"type": "error", "source": OVERLAY_SOURCE, "message": message}])
