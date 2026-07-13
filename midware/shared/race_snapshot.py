from __future__ import annotations

import time
from typing import Any


def _float(frame: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(frame.get(key, default))
    except (TypeError, ValueError):
        return default


def _int(frame: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(float(frame.get(key, default)))
    except (TypeError, ValueError):
        return default


def build_race_snapshot(
    telemetry: dict[str, Any] | None,
    rankings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the canonical compact race state shared by all features."""
    if not telemetry:
        return {
            "available": False,
            "session_id": None,
            "sim_time": None,
            "updated_at": None,
            "car": None,
            "rankings": rankings or [],
        }

    session_id = telemetry.get("_session_id", 1)
    updated_at = telemetry.get("_received_at") or time.time()
    snapshot = {
        "available": True,
        "session_id": session_id,
        "sim_time": round(_float(telemetry, "sim_time"), 3),
        "updated_at": round(float(updated_at), 3),
        "car": {
            "player": _int(telemetry, "player"),
            "lap": _int(telemetry, "lap"),
            "race_pos": _int(telemetry, "racePos"),
            "speed": round(_float(telemetry, "speedX"), 3),
            "speed_y": round(_float(telemetry, "speedY"), 3),
            "rpm": round(_float(telemetry, "rpm"), 3),
            "gear": _int(telemetry, "gear"),
            "track_pos": round(_float(telemetry, "trackPos"), 3),
            "damage": round(_float(telemetry, "damage"), 3),
            "fuel": round(_float(telemetry, "fuel"), 3),
            "cur_lap_time": round(_float(telemetry, "curLapTime"), 3),
            "last_lap_time": round(_float(telemetry, "lastLapTime"), 3),
            "dist_from_start": round(_float(telemetry, "distFromStart"), 3),
            "dist_raced": round(_float(telemetry, "distRaced"), 3),
            "throttle": round(_float(telemetry, "throttle"), 3),
            "brake": round(_float(telemetry, "brake"), 3),
            "steer": round(_float(telemetry, "steer"), 3),
        },
        "rankings": rankings or [],
    }
    return snapshot
