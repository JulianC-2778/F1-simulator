from __future__ import annotations

import time
from typing import Any


LEGACY_DEFAULT_SOURCES = {
    "ai_start": "commentary",
    "token": "commentary",
    "ai_done": "commentary",
    "error": "system",
    "config_updated": "system",
    "telemetry_update": "telemetry",
    "event_detected": "commentary",
    "connected": "system",
    "pong": "system",
}


def utc_timestamp() -> float:
    return round(time.time(), 3)


def display_message(
    *,
    source: str,
    content: str = "",
    title: str = "",
    level: str = "info",
    payload: dict[str, Any] | None = None,
    message_type: str = "message",
    **extra: Any,
) -> dict[str, Any]:
    message = {
        "type": message_type,
        "source": source,
        "level": level,
        "title": title,
        "content": content,
        "payload": payload or {},
        "timestamp": utc_timestamp(),
    }
    message.update(extra)
    return message


def normalize_outbound_message(message: dict[str, Any]) -> dict[str, Any]:
    """Add shared metadata while preserving legacy message compatibility."""
    normalized = dict(message)
    msg_type = str(normalized.get("type") or "message")
    normalized["type"] = msg_type
    normalized.setdefault("source", LEGACY_DEFAULT_SOURCES.get(msg_type, "system"))
    normalized.setdefault("timestamp", utc_timestamp())
    if msg_type == "error":
        normalized.setdefault("level", "error")
    else:
        normalized.setdefault("level", "info")
    return normalized

