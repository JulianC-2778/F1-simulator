from __future__ import annotations

import time
import uuid
from threading import Lock
from typing import Any

from midware.schemas.output import OutputMessageV1


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

ALLOWED_SOURCES = {"system", "telemetry", "commentary", "engineer", "coach", "bot"}
CANONICAL_TYPES = {
    "ai_start": "ai.start",
    "token": "ai.token",
    "ai_done": "ai.done",
    "event_detected": "event.detected",
    "telemetry_update": "telemetry.update",
    "config_updated": "status.update",
    "message": "status.update",
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
    return default_output_bus.normalize(message)


class OutputBus:
    """Create V1 envelopes while retaining the renderer's legacy fields."""

    def __init__(self) -> None:
        self._active_requests: dict[str, str] = {}
        self._sequences: dict[str, int] = {}
        self._lock = Lock()

    def normalize(self, message: dict[str, Any]) -> dict[str, Any]:
        """Add protocol metadata while preserving the legacy ``type`` value."""
        normalized = dict(message)
        msg_type = str(normalized.get("type") or "message")
        source = str(normalized.get("source") or LEGACY_DEFAULT_SOURCES.get(msg_type, "system"))
        if source not in ALLOWED_SOURCES:
            source = "system"

        with self._lock:
            request_id = str(normalized.get("request_id") or "")
            if not request_id:
                request_id = self._request_id_for(source, msg_type)
            sequence = normalized.get("sequence")
            if sequence is None:
                sequence = self._sequences.get(request_id, 0)
                self._sequences[request_id] = int(sequence) + 1

        content = normalized.get("content")
        if content is None:
            content = normalized.get("text") or normalized.get("message") or ""
        content = str(content)

        normalized.update(
            {
                "version": 1,
                "type": msg_type,
                "protocol_type": CANONICAL_TYPES.get(msg_type, msg_type),
                "source": source,
                "request_id": request_id,
                "sequence": int(sequence),
                "timestamp": float(normalized.get("timestamp") or utc_timestamp()),
                "level": str(normalized.get("level") or ("error" if msg_type == "error" else "info")),
                "content": content,
                "payload": dict(normalized.get("payload") or {}),
            }
        )
        if msg_type == "token":
            normalized.setdefault("text", content)

        # Validate only the canonical envelope; extra legacy keys stay untouched.
        OutputMessageV1.model_validate(
            {key: normalized[key] for key in OutputMessageV1.model_fields}
        )
        return normalized

    def _request_id_for(self, source: str, msg_type: str) -> str:
        if msg_type == "ai_start":
            request_id = str(uuid.uuid4())
            self._active_requests[source] = request_id
            return request_id
        request_id = self._active_requests.get(source)
        if request_id is None:
            request_id = str(uuid.uuid4())
        if msg_type in {"ai_done", "error"}:
            self._active_requests.pop(source, None)
        return request_id


default_output_bus = OutputBus()


def _legacy_normalize_outbound_message(message: dict[str, Any]) -> dict[str, Any]:
    """Kept only as documentation of the pre-V1 compatibility behavior."""
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
