from __future__ import annotations

import json
from typing import Any

from telemetry_common import (
    compact_opponent_profile,
    compact_track_profile,
    latest_state_payload,
    select_recent_frames,
    summarize_frames,
)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\n", " ").replace("\r", " ").split()).strip()


def truncate_text(value: Any, limit: int) -> str:
    text = clean_text(value)
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def safe_min(values: list[float], default: float = 0.0) -> float:
    return min(values) if values else default


def midware_frame_to_common(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": safe_int(frame.get("seq")),
        "sim_time": safe_float(frame.get("sim_time")),
        "player": safe_int(frame.get("player")),
        "lap": safe_int(frame.get("lap")),
        "x": safe_float(frame.get("x")),
        "y": safe_float(frame.get("y")),
        "yaw": safe_float(frame.get("yaw")),
        "accel_x": safe_float(frame.get("accel_x")),
        "accel_y": safe_float(frame.get("accel_y")),
        "steer": safe_float(frame.get("steer")),
        "throttle": safe_float(frame.get("throttle")),
        "brake": safe_float(frame.get("brake")),
        "clutch": safe_float(frame.get("clutch")),
        "angle": safe_float(frame.get("angle")),
        "cur_lap_time": safe_float(frame.get("curLapTime")),
        "damage": safe_float(frame.get("damage")),
        "dist_from_start": safe_float(frame.get("distFromStart")),
        "dist_raced": safe_float(frame.get("distRaced")),
        "fuel": safe_float(frame.get("fuel")),
        "gear": safe_int(frame.get("gear")),
        "last_lap_time": safe_float(frame.get("lastLapTime")),
        "race_pos": safe_int(frame.get("racePos")),
        "rpm": safe_float(frame.get("rpm")),
        "speed_x": safe_float(frame.get("speedX")),
        "speed_y": safe_float(frame.get("speedY")),
        "speed_z": safe_float(frame.get("speedZ")),
        "track_pos": safe_float(frame.get("trackPos")),
        "z": safe_float(frame.get("z")),
        "opponents": [safe_float(frame.get(f"opponent_{i}"), 200.0) for i in range(36)],
        "track": [safe_float(frame.get(f"track_{i}"), -1.0) for i in range(19)],
        "wheel_spin_vel": [safe_float(frame.get(f"wheelSpinVel_{i}")) for i in range(4)],
        "focus": [safe_float(frame.get(f"focus_{i}"), -1.0) for i in range(5)],
    }


def compact_live_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "duration": round(summary.get("duration", 0.0), 3),
        "avg_speed": round(summary.get("avg_speed", 0.0), 3),
        "max_speed": round(summary.get("max_speed", 0.0), 3),
        "avg_throttle": round(summary.get("avg_throttle", 0.0), 3),
        "avg_brake": round(summary.get("avg_brake", 0.0), 3),
        "brake_events": int(summary.get("brake_events", 0)),
        "off_track_moments": int(summary.get("off_track_moments", 0)),
        "edge_pressure_moments": int(summary.get("edge_pressure_moments", 0)),
        "track_pos_stddev": round(summary.get("track_pos_stddev", 0.0), 3),
        "steering_stddev": round(summary.get("steering_stddev", 0.0), 3),
        "nearest_opponent_now": round(summary.get("nearest_opponent_now", 200.0), 3),
        "damage_delta": round(summary.get("damage_delta", 0.0), 3),
    }


def build_rule_feedback(frames: list[dict[str, Any]]) -> dict[str, Any]:
    latest = frames[-1]
    summary = summarize_frames(frames)
    track_profile = compact_track_profile(latest["track"])
    opponent_profile = compact_opponent_profile(latest["opponents"])

    pit_advice = "No pit stop needed yet."
    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        pit_advice = "Pit now. Fuel or damage is already in the danger zone."
    elif latest["fuel"] < 10.0 or latest["damage"] > 25.0:
        pit_advice = "Pit soon. Fuel or damage is trending risky."

    if opponent_profile["front_gap"] < 8.0 and latest["speed_x"] > 60.0:
        return {
            "state_id": "collision_risk",
            "headline": "Traffic Alert",
            "focus_area": "traffic",
            "priority": "high",
            "analysis": f"Front gap is only {opponent_profile['front_gap']:.1f} m, so the overtake or braking window is tight.",
            "action": "Defend the inside and brake slightly earlier to avoid contact with the car ahead.",
            "pit_advice": pit_advice,
            "confidence": 0.95,
        }

    if abs(latest["track_pos"]) > 1.0:
        side = "left" if latest["track_pos"] < 0 else "right"
        return {
            "state_id": "off_track_recovery",
            "headline": "Off Track Risk",
            "focus_area": "cornering",
            "priority": "high",
            "analysis": f"The car is already beyond the {side} track edge with track position {latest['track_pos']:.2f}.",
            "action": "Straighten the steering, ease off the throttle, and rejoin the track smoothly before pushing again.",
            "pit_advice": pit_advice,
            "confidence": 0.98,
        }

    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        return {
            "state_id": "pit_now",
            "headline": "Pit Window Open",
            "focus_area": "pit_strategy",
            "priority": "high",
            "analysis": "Fuel or damage has crossed the local safety threshold.",
            "action": "Commit to a pit stop at the next safe opportunity.",
            "pit_advice": pit_advice,
            "confidence": 0.97,
        }

    if track_profile["center_opening"] < 25.0 and latest["brake"] < 0.08 and latest["speed_x"] > 90.0:
        return {
            "state_id": "late_braking",
            "headline": "Braking Point",
            "focus_area": "braking",
            "priority": "medium",
            "analysis": "The road ahead is tightening, but brake pressure is still very low for the current speed.",
            "action": "Brake earlier for the next corner and finish most of the braking before turn-in.",
            "pit_advice": pit_advice,
            "confidence": 0.82,
        }

    if summary.get("steering_stddev", 0.0) > 0.35 and abs(latest["track_pos"]) > 0.6:
        return {
            "state_id": "unstable_line",
            "headline": "Line Stability",
            "focus_area": "cornering",
            "priority": "medium",
            "analysis": "Steering corrections are high while the car is already close to the edge of the track.",
            "action": "Use one smoother steering input and let the car breathe back toward the center on exit.",
            "pit_advice": pit_advice,
            "confidence": 0.80,
        }

    if summary.get("avg_throttle", 0.0) < 0.35 and track_profile["center_opening"] > 60.0 and latest["speed_x"] > 70.0:
        return {
            "state_id": "throttle_hesitation",
            "headline": "Throttle Timing",
            "focus_area": "throttle",
            "priority": "medium",
            "analysis": "There is open road ahead, but throttle application over the last window has stayed conservative.",
            "action": "Start squeezing on the throttle earlier once the steering wheel begins to unwind.",
            "pit_advice": pit_advice,
            "confidence": 0.76,
        }

    return {
        "state_id": "stable_rhythm",
        "headline": "Rhythm Check",
        "focus_area": "cornering",
        "priority": "low",
        "analysis": "The current window looks stable, with no urgent danger signal from the local rules.",
        "action": "Keep building rhythm and focus on a clean entry-to-exit line.",
        "pit_advice": pit_advice,
        "confidence": 0.62,
    }


def series_points(frames: list[dict[str, Any]], key: str) -> list[dict[str, float]]:
    return [
        {
            "sim_time": round(safe_float(frame.get("sim_time")), 3),
            "value": round(safe_float(frame.get(key)), 3),
        }
        for frame in frames
    ]


def overlay_key(rule_feedback: dict[str, Any]) -> str:
    parts = [
        clean_text(rule_feedback.get("state_id") or "stable_rhythm"),
        clean_text(rule_feedback.get("focus_area")),
        clean_text(rule_feedback.get("priority")),
        clean_text(rule_feedback.get("action")),
        clean_text(rule_feedback.get("pit_advice")),
    ]
    return "|".join(parts)


def overlay_payload(
    latest: dict[str, Any],
    summary: dict[str, Any],
    track_profile: dict[str, Any],
    opponent_profile: dict[str, Any],
    rule_feedback: dict[str, Any],
) -> dict[str, Any]:
    return {
        "state_id": rule_feedback.get("state_id", "stable_rhythm"),
        "focus_area": rule_feedback.get("focus_area", "cornering"),
        "priority": rule_feedback.get("priority", "medium"),
        "headline": rule_feedback.get("headline", "Guidance"),
        "action": rule_feedback.get("action", ""),
        "pit_advice": rule_feedback.get("pit_advice", "No pit stop needed yet."),
        "rule_reason": rule_feedback.get("analysis", ""),
        "latest_state": {
            "lap": latest["lap"],
            "speed_x": round(latest["speed_x"], 3),
            "gear": latest["gear"],
            "throttle": round(latest["throttle"], 3),
            "brake": round(latest["brake"], 3),
            "track_pos": round(latest["track_pos"], 3),
            "damage": round(latest["damage"], 3),
            "fuel": round(latest["fuel"], 3),
        },
        "window_summary": {
            "avg_speed": round(summary.get("avg_speed", 0.0), 3),
            "avg_throttle": round(summary.get("avg_throttle", 0.0), 3),
            "avg_brake": round(summary.get("avg_brake", 0.0), 3),
            "brake_events": int(summary.get("brake_events", 0)),
            "track_pos_stddev": round(summary.get("track_pos_stddev", 0.0), 3),
            "steering_stddev": round(summary.get("steering_stddev", 0.0), 3),
            "damage_delta": round(summary.get("damage_delta", 0.0), 3),
        },
        "track_profile": track_profile,
        "opponent_profile": opponent_profile,
    }


def overlay_prompt(payload: dict[str, Any]) -> str:
    return f"""You are supplementing a rule-based TORCS telemetry dashboard.
The rule guidance is already fixed. Do not replace the action, headline, focus area, or pit advice.
Your only job is to add a short, useful explanation for the dashboard.

Rules:
1. Output one valid JSON object only.
2. Use English only.
3. Keep the response concise and telemetry-grounded.
4. "analysis" should explain why the current rule guidance makes sense.
5. "coach_note" should be one short supporting sentence for the driver.

Return this schema:
{{
  "analysis": "1-2 short sentences",
  "coach_note": "one short supporting sentence"
}}

Payload:
{json.dumps(payload, ensure_ascii=True)}"""


def pending_overlay() -> dict[str, Any]:
    return {
        "status": "pending",
        "source": "model_overlay",
        "analysis": "",
        "coach_note": "",
        "updated_at": None,
        "error": "",
    }


def empty_dashboard(
    window_seconds: float,
    history_seconds: float,
    *,
    error: str = "",
    upstream_ok: bool = True,
) -> dict[str, Any]:
    return {
        "status": {
            "has_telemetry": False,
            "window_seconds": window_seconds,
            "history_seconds": history_seconds,
            "frame_count": 0,
            "upstream_ok": upstream_ok,
            "error": error,
        },
        "latest_state": None,
        "window_summary": None,
        "track_profile": None,
        "opponent_profile": None,
        "guidance": None,
        "signals": [],
        "history": {
            "speed_x": [],
            "throttle": [],
            "brake": [],
            "track_pos": [],
            "rpm": [],
        },
    }


def build_dashboard_payload(
    raw_frames: list[dict[str, Any]],
    *,
    window_seconds: float = 6.0,
    history_seconds: float = 16.0,
) -> dict[str, Any]:
    if not raw_frames:
        return empty_dashboard(window_seconds, history_seconds)

    common_frames = [midware_frame_to_common(frame) for frame in raw_frames]
    if not common_frames:
        return empty_dashboard(window_seconds, history_seconds)

    live_frames = select_recent_frames(common_frames, window_seconds) or common_frames
    history_frames = select_recent_frames(common_frames, history_seconds) or common_frames
    latest = live_frames[-1]
    summary = summarize_frames(live_frames)
    rule_feedback = build_rule_feedback(live_frames)
    track_profile = compact_track_profile(latest["track"])
    opponent_profile = compact_opponent_profile(latest["opponents"])
    overlay_request = overlay_payload(latest, summary, track_profile, opponent_profile, rule_feedback)

    track_pos = latest["track_pos"]
    signals = [
        {
            "label": "Track Limit",
            "value": round(track_pos, 3),
            "display": f"{track_pos:+.2f}",
            "tone": "danger" if abs(track_pos) > 1.0 else "warn" if abs(track_pos) > 0.8 else "good",
        },
        {
            "label": "Front Gap",
            "value": opponent_profile["front_gap"],
            "display": f"{opponent_profile['front_gap']:.1f} m",
            "tone": "danger" if opponent_profile["front_gap"] < 8.0 else "warn" if opponent_profile["front_gap"] < 15.0 else "good",
        },
        {
            "label": "Fuel Reserve",
            "value": latest["fuel"],
            "display": f"{latest['fuel']:.1f} L",
            "tone": "danger" if latest["fuel"] < 6.0 else "warn" if latest["fuel"] < 10.0 else "good",
        },
        {
            "label": "Damage Load",
            "value": latest["damage"],
            "display": f"{latest['damage']:.1f}",
            "tone": "danger" if latest["damage"] > 40.0 else "warn" if latest["damage"] > 25.0 else "good",
        },
    ]

    return {
        "status": {
            "has_telemetry": True,
            "window_seconds": window_seconds,
            "history_seconds": history_seconds,
            "frame_count": len(history_frames),
            "latest_sim_time": round(latest["sim_time"], 3),
            "upstream_ok": True,
            "error": "",
        },
        "latest_state": latest_state_payload(latest),
        "window_summary": compact_live_summary(summary),
        "track_profile": track_profile,
        "opponent_profile": opponent_profile,
        "guidance": {
            "analysis_type": "live_window",
            "source": "rule_engine",
            "sim_time": round(latest["sim_time"], 3),
            "state_id": rule_feedback.get("state_id", "stable_rhythm"),
            "headline": rule_feedback["headline"],
            "focus_area": rule_feedback["focus_area"],
            "priority": rule_feedback["priority"],
            "analysis": rule_feedback["analysis"],
            "action": rule_feedback["action"],
            "pit_advice": rule_feedback["pit_advice"],
            "confidence": round(safe_float(rule_feedback.get("confidence"), 0.0), 2),
            "async_overlay": pending_overlay(),
        },
        "signals": signals,
        "history": {
            "speed_x": series_points(history_frames, "speed_x"),
            "throttle": series_points(history_frames, "throttle"),
            "brake": series_points(history_frames, "brake"),
            "track_pos": series_points(history_frames, "track_pos"),
            "rpm": series_points(history_frames, "rpm"),
        },
        "_overlay_request": overlay_request,
        "_overlay_cache_key": overlay_key(rule_feedback),
    }
