#!/usr/bin/env python3
"""
Feature 2: Granite-powered telemetry analysis and driving guidance.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from telemetry_common import (
    DEFAULT_MODEL_BASE_URL,
    DEFAULT_MODEL_NAME,
    LatestTaskRunner,
    TelemetryBuffer,
    chat_completion_text,
    compact_opponent_profile,
    compact_track_profile,
    connect_openai_compatible_model,
    env_flag,
    extract_json_object,
    latest_state_payload,
    normalize_text_key,
    print_connection_banner,
    select_recent_frames,
    speak_text,
    summarize_frames,
)


UDP_PORT = int(os.getenv("TORCS_ANALYZER_UDP_PORT", "3101"))
RETENTION_SECONDS = float(os.getenv("TORCS_ANALYZER_RETENTION_SECONDS", "180"))
LIVE_INTERVAL = float(os.getenv("TORCS_ANALYZER_INTERVAL", "2.0"))
LIVE_WINDOW = float(os.getenv("TORCS_ANALYZER_WINDOW_SECONDS", "5.0"))
LOOP_INTERVAL = float(os.getenv("TORCS_ANALYZER_LOOP_INTERVAL", "0.4"))
MAX_STALE_SECONDS = float(os.getenv("TORCS_ANALYZER_MAX_STALE_SECONDS", "2.5"))
ACTION_DEDUPE_SECONDS = float(os.getenv("TORCS_ANALYZER_DEDUPE_SECONDS", "8.0"))
MODEL_TIMEOUT_SECONDS = float(os.getenv("TORCS_ANALYZER_MODEL_TIMEOUT", "24.0"))
LAP_RESULT_MAX_STALE_SECONDS = float(os.getenv("TORCS_ANALYZER_LAP_MAX_STALE_SECONDS", "120.0"))
LIVE_MODEL_COOLDOWN_SECONDS = float(os.getenv("TORCS_ANALYZER_MODEL_COOLDOWN", "6.0"))
LIVE_MODEL_REPEAT_SECONDS = float(os.getenv("TORCS_ANALYZER_MODEL_REPEAT_SECONDS", "12.0"))
ENABLE_TTS = env_flag("TORCS_ANALYZER_TTS", False)
ENABLE_RULE_FASTPATH = env_flag("TORCS_ANALYZER_RULE_FASTPATH", True)
TTS_VOICE = os.getenv("TORCS_ANALYZER_TTS_VOICE", "en-us")
TTS_RATE = int(os.getenv("TORCS_ANALYZER_TTS_RATE", "160"))
PULSE_SERVER = os.getenv("TORCS_ANALYZER_PULSE_SERVER", "/mnt/wslg/PulseServer")
MODEL_BASE_URL = os.getenv("TORCS_ANALYZER_BASE_URL", DEFAULT_MODEL_BASE_URL)
MODEL_NAME = os.getenv("TORCS_ANALYZER_MODEL", DEFAULT_MODEL_NAME)
LIVE_MAX_TOKENS = int(os.getenv("TORCS_ANALYZER_LIVE_MAX_TOKENS", "120"))
LAP_MAX_TOKENS = int(os.getenv("TORCS_ANALYZER_LAP_MAX_TOKENS", "256"))
VALID_FOCUS_AREAS = {"braking", "cornering", "throttle", "traffic", "pit_strategy"}
VALID_PRIORITIES = {"low", "medium", "high"}
PIT_DECISIONS = ("Pit now", "Pit soon", "Stay out")
HEADLINE_TEXT_LIMIT = 24
ANALYSIS_TEXT_LIMIT = 160
ACTION_TEXT_LIMIT = 96
TIP_TEXT_LIMIT = 96


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\n", " ").replace("\r", " ").split()).strip()


def _truncate_text(value: Any, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _normalize_focus_area(value: Any, fallback: str = "cornering") -> str:
    text = _clean_text(value).lower()
    aliases = {
        "braking": "braking",
        "brake": "braking",
        "cornering": "cornering",
        "corner": "cornering",
        "line": "cornering",
        "throttle": "throttle",
        "power": "throttle",
        "traffic": "traffic",
        "battle": "traffic",
        "opponent": "traffic",
        "pit_strategy": "pit_strategy",
        "pit": "pit_strategy",
        "strategy": "pit_strategy",
    }
    if text in aliases:
        return aliases[text]
    if text in VALID_FOCUS_AREAS:
        return text
    return fallback if fallback in VALID_FOCUS_AREAS else "cornering"


def _normalize_priority(value: Any, fallback: str = "medium") -> str:
    text = _clean_text(value).lower()
    aliases = {
        "high": "high",
        "urgent": "high",
        "medium": "medium",
        "normal": "medium",
        "low": "low",
    }
    if text in aliases:
        return aliases[text]
    if text in VALID_PRIORITIES:
        return text
    return fallback if fallback in VALID_PRIORITIES else "medium"


def _normalize_confidence(value: Any, fallback: float = 0.72) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(1.0, round(number, 3)))


def _normalize_state_id(value: Any, fallback: str = "stable_rhythm") -> str:
    text = _clean_text(value).lower().replace("-", "_").replace(" ", "_")
    filtered = "".join(char for char in text if char.isalnum() or char == "_").strip("_")
    return filtered or fallback


def _normalize_metric_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3)
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return _clean_text(value)


def _normalize_metrics(metrics: Any) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        return {}
    return {
        str(key): _normalize_metric_value(value)
        for key, value in metrics.items()
        if value is not None and _clean_text(key)
    }


def _focus_headline(focus_area: str) -> str:
    return {
        "braking": "Braking Note",
        "cornering": "Cornering Note",
        "throttle": "Throttle Note",
        "traffic": "Traffic Note",
        "pit_strategy": "Pit Note",
    }.get(focus_area, "Driving Note")


def _default_action(focus_area: str) -> str:
    return {
        "braking": "Brake earlier for the next corner and finish the main deceleration before turn-in.",
        "cornering": "Reduce extra steering corrections and keep a smoother line from entry to exit.",
        "throttle": "Get on the throttle earlier and more smoothly once the wheel starts to unwind.",
        "traffic": "Stabilize the line and braking point first, then manage the traffic around you.",
        "pit_strategy": "Watch fuel and damage closely and prepare for the next safe pit window.",
    }.get(focus_area, "Keep the rhythm clean and prioritize a stable line.")


def _default_analysis(focus_area: str, priority: str) -> str:
    details = {
        "braking": "This window needs more attention on braking point and deceleration timing.",
        "cornering": "This window needs more attention on line stability and steering continuity.",
        "throttle": "This window needs more attention on exit throttle timing and smoothness.",
        "traffic": "Cars nearby make traffic management the main priority in this window.",
        "pit_strategy": "Fuel or damage is getting close to a pit strategy decision point.",
    }
    level = {"high": "Priority is high.", "medium": "Priority is medium.", "low": "Priority is low."}[priority]
    return f"{details.get(focus_area, 'This window mainly needs a stable rhythm.')} {level}"


def _default_lap_tip(focus_area: str, tip_type: str) -> str:
    defaults = {
        "braking": "Finish the main braking phase earlier before turn-in.",
        "cornering": "Reduce second corrections and hold a cleaner apex line.",
        "throttle": "Feed throttle in smoothly once the steering begins to open.",
    }
    if tip_type == "braking":
        return defaults["braking"]
    if tip_type == "cornering":
        return defaults["cornering"]
    if tip_type == "throttle":
        return defaults["throttle"]
    return _default_action(focus_area)


def _normalize_pit_advice(value: Any, fallback: str = "Stay out") -> str:
    text = _clean_text(value).lower()
    if not text:
        return fallback if fallback in PIT_DECISIONS else "Stay out"

    immediate_tokens = ("pit now", "box now", "now", "danger", "critical")
    soon_tokens = ("pit soon", "soon", "later", "risky", "window", "prepare")
    hold_tokens = ("stay out", "no pit", "not yet", "hold")

    if any(token in text for token in immediate_tokens):
        return "Pit now"
    if any(token in text for token in soon_tokens):
        return "Pit soon"
    if any(token in text for token in hold_tokens):
        return "Stay out"
    if fallback in PIT_DECISIONS:
        return fallback
    return "Stay out"


def _build_feedback(
    *,
    state_id: str,
    headline: str,
    focus_area: str,
    priority: str,
    analysis: str,
    action: str,
    pit_advice: str,
    confidence: float,
    metrics: dict[str, Any] | None = None,
    fast_path: bool = False,
    model_worthy: bool = True,
) -> dict[str, Any]:
    normalized_state = _normalize_state_id(state_id)
    normalized_focus = _normalize_focus_area(focus_area)
    normalized_priority = _normalize_priority(priority)
    normalized_metrics = _normalize_metrics(metrics)
    normalized_pit_advice = _normalize_pit_advice(pit_advice)
    return {
        "state_id": normalized_state,
        "state_reason": _truncate_text(analysis, ANALYSIS_TEXT_LIMIT),
        "state_metrics": normalized_metrics,
        "state_signature": f"{normalized_state}|{normalized_priority}|{normalized_pit_advice}",
        "headline": _truncate_text(headline, HEADLINE_TEXT_LIMIT) or _focus_headline(normalized_focus),
        "focus_area": normalized_focus,
        "priority": normalized_priority,
        "analysis": _truncate_text(analysis, ANALYSIS_TEXT_LIMIT) or _default_analysis(normalized_focus, normalized_priority),
        "action": _truncate_text(action, ACTION_TEXT_LIMIT) or _default_action(normalized_focus),
        "pit_advice": normalized_pit_advice,
        "confidence": _normalize_confidence(confidence),
        "source": "rules",
        "fast_path": fast_path,
        "model_worthy": model_worthy,
    }


def _default_result(payload: dict[str, Any], raw_text: str = "") -> dict[str, Any]:
    state = payload.get("state", {}) if isinstance(payload.get("state"), dict) else {}
    rule_hint = payload.get("rule_hint", {}) if isinstance(payload.get("rule_hint"), dict) else {}
    analysis_type = str(payload.get("analysis_type", "live_window"))
    focus_area = _normalize_focus_area(state.get("focus_area") or rule_hint.get("focus_area"), "cornering")
    priority = _normalize_priority(rule_hint.get("priority"), "medium")
    state_id = _normalize_state_id(state.get("id") or rule_hint.get("state_id") or "stable_rhythm")
    state_reason = _truncate_text(
        state.get("reason") or rule_hint.get("analysis") or _default_analysis(focus_area, priority),
        ANALYSIS_TEXT_LIMIT,
    ) or _default_analysis(focus_area, priority)
    state_metrics = _normalize_metrics(state.get("metrics") or {})
    action = _truncate_text(rule_hint.get("action") or _default_action(focus_area), ACTION_TEXT_LIMIT) or _default_action(focus_area)
    pit_advice = _normalize_pit_advice(rule_hint.get("pit_advice"), "Stay out")
    analysis_source = raw_text or state_reason
    result = {
        "state_id": state_id,
        "state_reason": state_reason,
        "state_metrics": state_metrics,
        "headline": _focus_headline(focus_area),
        "focus_area": focus_area,
        "priority": priority,
        "analysis": _truncate_text(analysis_source, ANALYSIS_TEXT_LIMIT) or _default_analysis(focus_area, priority),
        "action": action,
        "pit_advice": pit_advice,
        "confidence": 0.72 if priority == "medium" else 0.84 if priority == "high" else 0.6,
        "source": "rules",
        "braking_tip": "",
        "cornering_tip": "",
        "throttle_tip": "",
    }
    if analysis_type == "lap_review":
        result["braking_tip"] = _default_lap_tip(focus_area, "braking")
        result["cornering_tip"] = _default_lap_tip(focus_area, "cornering")
        result["throttle_tip"] = _default_lap_tip(focus_area, "throttle")
    return result


def _normalize_model_result(
    payload: dict[str, Any],
    parsed: dict[str, Any] | None,
    raw_text: str,
) -> dict[str, Any]:
    result = _default_result(payload, raw_text=raw_text if parsed is None else "")
    if not parsed:
        return result

    result["focus_area"] = _normalize_focus_area(parsed.get("focus_area"), result["focus_area"])
    result["priority"] = _normalize_priority(parsed.get("priority"), result["priority"])
    result["headline"] = _truncate_text(
        parsed.get("headline") or _focus_headline(result["focus_area"]),
        HEADLINE_TEXT_LIMIT,
    ) or _focus_headline(result["focus_area"])
    result["analysis"] = _truncate_text(
        parsed.get("analysis") or result["analysis"],
        ANALYSIS_TEXT_LIMIT,
    ) or result["analysis"]
    result["action"] = _truncate_text(
        parsed.get("action") or result["action"],
        ACTION_TEXT_LIMIT,
    ) or result["action"]
    result["pit_advice"] = _normalize_pit_advice(parsed.get("pit_advice"), result["pit_advice"])
    result["confidence"] = _normalize_confidence(parsed.get("confidence"), result["confidence"])
    result["source"] = "model"

    if str(payload.get("analysis_type", "")) == "lap_review":
        result["braking_tip"] = _truncate_text(
            parsed.get("braking_tip") or result["braking_tip"],
            TIP_TEXT_LIMIT,
        ) or result["braking_tip"]
        result["cornering_tip"] = _truncate_text(
            parsed.get("cornering_tip") or result["cornering_tip"],
            TIP_TEXT_LIMIT,
        ) or result["cornering_tip"]
        result["throttle_tip"] = _truncate_text(
            parsed.get("throttle_tip") or result["throttle_tip"],
            TIP_TEXT_LIMIT,
        ) or result["throttle_tip"]

    return result


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


def compact_lap_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "duration": round(summary.get("duration", 0.0), 3),
        "avg_speed": round(summary.get("avg_speed", 0.0), 3),
        "max_speed": round(summary.get("max_speed", 0.0), 3),
        "avg_throttle": round(summary.get("avg_throttle", 0.0), 3),
        "avg_brake": round(summary.get("avg_brake", 0.0), 3),
        "avg_rpm": round(summary.get("avg_rpm", 0.0), 3),
        "peak_rpm": round(summary.get("peak_rpm", 0.0), 3),
        "brake_events": int(summary.get("brake_events", 0)),
        "throttle_lifts": int(summary.get("throttle_lifts", 0)),
        "off_track_moments": int(summary.get("off_track_moments", 0)),
        "edge_pressure_moments": int(summary.get("edge_pressure_moments", 0)),
        "track_pos_stddev": round(summary.get("track_pos_stddev", 0.0), 3),
        "steering_stddev": round(summary.get("steering_stddev", 0.0), 3),
        "angle_stddev": round(summary.get("angle_stddev", 0.0), 3),
        "nearest_opponent_window": round(summary.get("nearest_opponent_window", 200.0), 3),
        "damage_delta": round(summary.get("damage_delta", 0.0), 3),
    }


def build_rule_feedback(frames: list[dict[str, Any]]) -> dict[str, Any]:
    latest = frames[-1]
    summary = summarize_frames(frames)
    track_profile = compact_track_profile(latest["track"])
    opponent_profile = compact_opponent_profile(latest["opponents"])

    pit_advice = "Stay out"
    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        pit_advice = "Pit now"
    elif latest["fuel"] < 10.0 or latest["damage"] > 25.0:
        pit_advice = "Pit soon"

    if opponent_profile["front_gap"] < 8.0 and latest["speed_x"] > 60.0:
        return _build_feedback(
            state_id="collision_risk",
            headline="Traffic Alert",
            focus_area="traffic",
            priority="high",
            analysis=f"Front gap is only {opponent_profile['front_gap']:.1f} m, so the braking and overtake window is tight.",
            action="Cover the inside and brake slightly earlier to avoid contact with the car ahead.",
            pit_advice=pit_advice,
            confidence=0.95,
            metrics={
                "front_gap": opponent_profile["front_gap"],
                "speed_x": latest["speed_x"],
                "rear_gap": opponent_profile["rear_gap"],
            },
            fast_path=True,
        )

    if abs(latest["track_pos"]) > 1.0:
        side = "left" if latest["track_pos"] < 0 else "right"
        return _build_feedback(
            state_id="off_track_recovery",
            headline="Rejoin Cleanly",
            focus_area="cornering",
            priority="high",
            analysis=f"The car is already beyond the {side} track edge with track position {latest['track_pos']:.2f}.",
            action="Straighten the wheel, ease the throttle, and rejoin smoothly before pushing again.",
            pit_advice=pit_advice,
            confidence=0.98,
            metrics={
                "track_pos": latest["track_pos"],
                "steer": latest["steer"],
                "throttle": latest["throttle"],
            },
            fast_path=True,
        )

    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        return _build_feedback(
            state_id="pit_now",
            headline="Pit Window",
            focus_area="pit_strategy",
            priority="high",
            analysis="Fuel or damage has already crossed the local safety threshold.",
            action="Commit to a pit stop at the next safe opportunity.",
            pit_advice=pit_advice,
            confidence=0.97,
            metrics={
                "fuel": latest["fuel"],
                "damage": latest["damage"],
            },
            fast_path=True,
        )

    if latest["fuel"] < 10.0 or latest["damage"] > 25.0:
        return _build_feedback(
            state_id="pit_prepare",
            headline="Pit Soon",
            focus_area="pit_strategy",
            priority="medium",
            analysis="Fuel or damage is trending risky, so the next safe stop window matters.",
            action="Start planning a stop and avoid unnecessary damage before the next safe window.",
            pit_advice=pit_advice,
            confidence=0.84,
            metrics={
                "fuel": latest["fuel"],
                "damage": latest["damage"],
            },
        )

    if track_profile["center_opening"] < 25.0 and latest["brake"] < 0.08 and latest["speed_x"] > 90.0:
        return _build_feedback(
            state_id="late_braking",
            headline="Brake Earlier",
            focus_area="braking",
            priority="medium",
            analysis="The road ahead is tightening, but brake pressure is still low for the current speed.",
            action="Brake earlier for the next corner and finish most of the braking before turn-in.",
            pit_advice=pit_advice,
            confidence=0.82,
            metrics={
                "speed_x": latest["speed_x"],
                "brake": latest["brake"],
                "center_opening": track_profile["center_opening"],
            },
        )

    if summary.get("steering_stddev", 0.0) > 0.35 and abs(latest["track_pos"]) > 0.6:
        return _build_feedback(
            state_id="unstable_line",
            headline="Line Stability",
            focus_area="cornering",
            priority="medium",
            analysis="Steering corrections are high while the car is already running close to the track edge.",
            action="Use one cleaner steering input and let the car breathe back toward the middle on exit.",
            pit_advice=pit_advice,
            confidence=0.8,
            metrics={
                "track_pos": latest["track_pos"],
                "steering_stddev": summary.get("steering_stddev", 0.0),
                "track_pos_stddev": summary.get("track_pos_stddev", 0.0),
            },
        )

    if summary.get("avg_throttle", 0.0) < 0.35 and track_profile["center_opening"] > 60.0 and latest["speed_x"] > 70.0:
        return _build_feedback(
            state_id="throttle_hesitation",
            headline="Throttle Timing",
            focus_area="throttle",
            priority="medium",
            analysis="There is open road ahead, but throttle use over this window has stayed conservative.",
            action="Start squeezing on the throttle earlier once the steering begins to unwind.",
            pit_advice=pit_advice,
            confidence=0.76,
            metrics={
                "avg_throttle": summary.get("avg_throttle", 0.0),
                "speed_x": latest["speed_x"],
                "center_opening": track_profile["center_opening"],
            },
        )

    return _build_feedback(
        state_id="stable_rhythm",
        headline="Keep Rhythm",
        focus_area="cornering",
        priority="low",
        analysis="This window looks stable overall, with no urgent danger signal from the local rules.",
        action="Keep building rhythm and focus on a clean line from entry to exit.",
        pit_advice=pit_advice,
        confidence=0.62,
        metrics={
            "avg_speed": summary.get("avg_speed", 0.0),
            "track_pos_stddev": summary.get("track_pos_stddev", 0.0),
            "nearest_gap": opponent_profile["nearest_gap"],
        },
        model_worthy=False,
    )


def compact_live_state(latest: dict[str, Any]) -> dict[str, Any]:
    return {
        "lap": latest["lap"],
        "gear": latest["gear"],
        "speed_x": round(latest["speed_x"], 3),
        "throttle": round(latest["throttle"], 3),
        "brake": round(latest["brake"], 3),
        "track_pos": round(latest["track_pos"], 3),
        "damage": round(latest["damage"], 3),
        "fuel": round(latest["fuel"], 3),
        "race_pos": latest["race_pos"],
    }


def compact_live_state_context(
    latest: dict[str, Any],
    summary: dict[str, Any],
    track_profile: dict[str, Any],
    opponent_profile: dict[str, Any],
    rule_feedback: dict[str, Any],
) -> dict[str, Any]:
    metrics = dict(rule_feedback.get("state_metrics", {}))
    metrics.setdefault("speed_x", round(latest["speed_x"], 3))
    metrics.setdefault("track_pos", round(latest["track_pos"], 3))
    metrics.setdefault("front_gap", round(opponent_profile["front_gap"], 3))
    metrics.setdefault("fuel", round(latest["fuel"], 3))
    metrics.setdefault("damage", round(latest["damage"], 3))
    metrics.setdefault("avg_throttle", round(summary.get("avg_throttle", 0.0), 3))
    metrics.setdefault("center_opening", round(track_profile["center_opening"], 3))
    return metrics


def build_live_payload(frames: list[dict[str, Any]], rule_feedback: dict[str, Any]) -> dict[str, Any]:
    latest = frames[-1]
    summary = summarize_frames(frames)
    track_profile = compact_track_profile(latest["track"])
    opponent_profile = compact_opponent_profile(latest["opponents"])
    return {
        "task": "telemetry_coaching",
        "analysis_type": "live_window",
        "window_seconds": LIVE_WINDOW,
        "state": {
            "id": rule_feedback["state_id"],
            "focus_area": rule_feedback["focus_area"],
            "priority": rule_feedback["priority"],
            "reason": rule_feedback["state_reason"],
            "metrics": compact_live_state_context(latest, summary, track_profile, opponent_profile, rule_feedback),
        },
        "latest_state": compact_live_state(latest),
        "rule_hint": {
            "state_id": rule_feedback["state_id"],
            "headline": rule_feedback["headline"],
            "focus_area": rule_feedback["focus_area"],
            "priority": rule_feedback["priority"],
            "action": rule_feedback["action"],
            "pit_advice": rule_feedback["pit_advice"],
        },
        "objectives": [
            "State classification has already been computed locally.",
            "Give one short instruction suitable for live radio coaching.",
            "Keep the reply useful, direct, and aligned with the supplied state.",
        ],
    }


def build_lap_payload(lap_frames: list[dict[str, Any]], completed_lap: int) -> dict[str, Any]:
    latest = lap_frames[-1]
    summary = summarize_frames(lap_frames)
    rule_feedback = build_rule_feedback(lap_frames)
    return {
        "task": "telemetry_coaching",
        "analysis_type": "lap_review",
        "lap_number": completed_lap,
        "state": {
            "id": rule_feedback["state_id"],
            "focus_area": rule_feedback["focus_area"],
            "priority": rule_feedback["priority"],
            "reason": rule_feedback["state_reason"],
            "metrics": rule_feedback.get("state_metrics", {}),
        },
        "latest_state": latest_state_payload(latest),
        "lap_summary": compact_lap_summary(summary),
        "rule_hint": {
            "state_id": rule_feedback["state_id"],
            "priority": rule_feedback["priority"],
            "focus_area": rule_feedback["focus_area"],
            "action": rule_feedback["action"],
            "pit_advice": rule_feedback["pit_advice"],
        },
        "objectives": [
            "Summarize lap rhythm in one sentence first.",
            "Give one braking tip, one cornering tip, and one throttle tip.",
            "State clearly whether the driver should be thinking about a pit stop.",
        ],
    }


def build_live_prompt(payload: dict[str, Any]) -> str:
    return f"""You are writing live radio coaching for a TORCS race engineer.
The local state classifier has already labeled the situation. Do not re-diagnose from scratch.
Use only the supplied state, compact metrics, and rule hint.

Rules:
1. Output one valid JSON object only.
2. All text fields must be in English.
3. Keep analysis to one short sentence.
4. Keep action short enough to say over radio immediately.
5. Stay aligned with the provided state unless the metrics clearly contradict it.
6. pit_advice must be exactly one of: Pit now, Pit soon, Stay out.
7. braking_tip, cornering_tip, and throttle_tip must be empty strings.

Fixed JSON schema:
{{
  "headline": "short title",
  "focus_area": "braking|cornering|throttle|traffic|pit_strategy",
  "priority": "low|medium|high",
  "analysis": "1 short sentence",
  "action": "one actionable instruction",
  "pit_advice": "Pit now|Pit soon|Stay out",
  "confidence": 0.0,
  "braking_tip": "",
  "cornering_tip": "",
  "throttle_tip": ""
}}

Live payload:
{json.dumps(payload, ensure_ascii=True)}"""


def build_lap_prompt(payload: dict[str, Any]) -> str:
    return f"""You are the race engineer for a TORCS student project. Base every recommendation only on the provided lap summary and state summary.

Rules:
1. Output one valid JSON object only.
2. All text fields must be in English.
3. Do not invent facts that are not supported by the payload.
4. Summarize the lap cleanly, then give one braking tip, one cornering tip, and one throttle tip.
5. pit_advice must be exactly one of: Pit now, Pit soon, Stay out.
6. For lap_review, braking_tip, cornering_tip, and throttle_tip must all be filled.

Fixed JSON schema:
{{
  "headline": "short title",
  "focus_area": "braking|cornering|throttle|traffic|pit_strategy",
  "priority": "low|medium|high",
  "analysis": "1-2 sentence explanation",
  "action": "one actionable instruction",
  "pit_advice": "Pit now|Pit soon|Stay out",
  "confidence": 0.0,
  "braking_tip": "braking suggestion",
  "cornering_tip": "cornering suggestion",
  "throttle_tip": "throttle suggestion"
}}

Lap payload:
{json.dumps(payload, ensure_ascii=True)}"""


def call_model(connection: Any, payload: dict[str, Any]) -> dict[str, Any]:
    analysis_type = str(payload.get("analysis_type", "live_window"))
    timeout_seconds = MODEL_TIMEOUT_SECONDS
    max_tokens = LAP_MAX_TOKENS
    temperature = 0.1
    if analysis_type == "live_window":
        timeout_seconds = min(MODEL_TIMEOUT_SECONDS, max(1.0, MAX_STALE_SECONDS - 0.3))
        max_tokens = LIVE_MAX_TOKENS
        temperature = 0.05

    prompt = build_live_prompt(payload) if analysis_type == "live_window" else build_lap_prompt(payload)

    try:
        text = chat_completion_text(
            connection,
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise race engineer assistant. Your output must be stable, structured, and easy for a program to parse.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout_seconds,
        )
    except Exception:
        fallback = _default_result(payload)
        fallback["headline"] = "Rule Fallback"
        fallback["analysis"] = _truncate_text(
            f"The model did not return in time, so the local rule guidance is being used for now. {fallback['analysis']}",
            70,
        )
        fallback["confidence"] = min(fallback["confidence"], 0.58)
        fallback["source"] = "fallback"
        return fallback
    parsed = extract_json_object(text)
    return _normalize_model_result(payload, parsed, text)


def print_analysis(prefix: str, result: dict[str, Any]) -> None:
    print(f"\n[{prefix}] {result.get('headline', 'Analysis')}")
    print(
        f"  State: {result.get('state_id', 'unknown')} | Focus: {result.get('focus_area', 'unknown')} | Priority: {result.get('priority', 'unknown')} | Source: {result.get('source', 'unknown')}"
    )
    print(f"  Why: {result.get('analysis', '')}")
    print(f"  Do: {result.get('action', '')}")
    print(f"  Pit: {result.get('pit_advice', '')}")
    if result.get("braking_tip") or result.get("cornering_tip") or result.get("throttle_tip"):
        print(f"  Braking: {result.get('braking_tip', '')}")
        print(f"  Cornering: {result.get('cornering_tip', '')}")
        print(f"  Throttle: {result.get('throttle_tip', '')}")


def should_request_live_model(
    rule_feedback: dict[str, Any],
    current_sim_time: float,
    last_signature: str,
    last_request_time: float,
) -> bool:
    if not rule_feedback.get("model_worthy", True):
        return False

    signature = str(rule_feedback.get("state_signature", "")).strip()
    if not signature:
        return False
    if signature != last_signature:
        return True

    if rule_feedback.get("priority") == "high":
        return current_sim_time - last_request_time >= LIVE_MODEL_COOLDOWN_SECONDS
    if rule_feedback.get("priority") == "medium":
        return current_sim_time - last_request_time >= LIVE_MODEL_REPEAT_SECONDS
    return False


def should_emit_text(history: dict[str, float], text: str, current_sim_time: float) -> bool:
    key = normalize_text_key(text)
    if not key:
        return True
    previous = history.get(key)
    if previous is not None and current_sim_time - previous < ACTION_DEDUPE_SECONDS:
        return False
    history[key] = current_sim_time
    return True


def handle_completed_result(
    collector: TelemetryBuffer,
    history: dict[str, float],
    result: Any,
) -> None:
    frames = collector.snapshot()
    if not frames:
        return
    current_sim_time = frames[-1]["sim_time"]
    requested_sim_time = float(result.task["requested_sim_time"])
    payload = result.task.get("payload", {})
    analysis_type = str(payload.get("analysis_type", "live_window"))
    stale_limit = MAX_STALE_SECONDS if analysis_type == "live_window" else LAP_RESULT_MAX_STALE_SECONDS
    if current_sim_time - requested_sim_time > stale_limit:
        return
    if result.error:
        print(f"[Analyzer Worker] {result.error}")
        return
    output = result.output
    action = str(output.get("action", "")).strip()
    if not action or not should_emit_text(history, action, current_sim_time):
        return
    print_analysis(result.task["label"], output)
    speak_text(
        action,
        enabled=ENABLE_TTS,
        voice=TTS_VOICE,
        rate=TTS_RATE,
        pulse_server=PULSE_SERVER,
    )


def main() -> None:
    connection = connect_openai_compatible_model(
        base_url=MODEL_BASE_URL,
        requested_model=MODEL_NAME,
    )
    collector = TelemetryBuffer(udp_port=UDP_PORT, retention_seconds=RETENTION_SECONDS)
    collector.start_background()

    live_worker = LatestTaskRunner(lambda task: call_model(connection, task["payload"]), "live-analyzer")
    lap_worker = LatestTaskRunner(lambda task: call_model(connection, task["payload"]), "lap-analyzer")

    last_live_request = 0.0
    last_live_model_signature = ""
    last_live_model_request_time = -10**9
    last_completed_lap = -1
    action_history: dict[str, float] = {}

    print_connection_banner(connection, "TORCS Telemetry Analyzer")
    print(f"Live interval: {LIVE_INTERVAL}s | Live window: {LIVE_WINDOW}s | Loop interval: {LOOP_INTERVAL}s")

    speak_text(
        "Telemetry analyzer is ready.",
        enabled=ENABLE_TTS,
        voice=TTS_VOICE,
        rate=TTS_RATE,
        pulse_server=PULSE_SERVER,
    )

    while True:
        try:
            time.sleep(LOOP_INTERVAL)
            frames = collector.snapshot()
            if len(frames) < 15:
                continue

            while True:
                completed = live_worker.pop_completed()
                if completed is None:
                    break
                handle_completed_result(collector, action_history, completed)

            while True:
                completed = lap_worker.pop_completed()
                if completed is None:
                    break
                handle_completed_result(collector, action_history, completed)

            recent_frames = select_recent_frames(frames, LIVE_WINDOW)
            if len(recent_frames) < 8:
                continue

            latest = recent_frames[-1]
            if latest["sim_time"] - last_live_request >= LIVE_INTERVAL:
                last_live_request = latest["sim_time"]
                rule_feedback = build_rule_feedback(recent_frames)

                if ENABLE_RULE_FASTPATH and rule_feedback.get("fast_path"):
                    if should_emit_text(action_history, rule_feedback["action"], latest["sim_time"]):
                        print_analysis(f"RULE LIVE {recent_frames[0]['sim_time']:.1f}s-{latest['sim_time']:.1f}s", rule_feedback)
                        speak_text(
                            rule_feedback["action"],
                            enabled=ENABLE_TTS,
                            voice=TTS_VOICE,
                            rate=TTS_RATE,
                            pulse_server=PULSE_SERVER,
                        )
                else:
                    if should_request_live_model(
                        rule_feedback,
                        latest["sim_time"],
                        last_live_model_signature,
                        last_live_model_request_time,
                    ):
                        live_payload = build_live_payload(recent_frames, rule_feedback)
                        submitted = live_worker.submit(
                            {
                                "label": f"LIVE {recent_frames[0]['sim_time']:.1f}s-{latest['sim_time']:.1f}s",
                                "requested_sim_time": latest["sim_time"],
                                "payload": live_payload,
                            },
                            priority=2 if rule_feedback["priority"] == "high" else 1,
                        )
                        if submitted:
                            last_live_model_signature = str(rule_feedback.get("state_signature", ""))
                            last_live_model_request_time = latest["sim_time"]

            completed_lap = latest["lap"] - 1
            if completed_lap > last_completed_lap and completed_lap >= 1:
                lap_frames = [frame for frame in frames if frame["lap"] == completed_lap]
                if len(lap_frames) >= 20:
                    lap_payload = build_lap_payload(lap_frames, completed_lap)
                    lap_worker.submit(
                        {
                            "label": f"LAP {completed_lap}",
                            "requested_sim_time": latest["sim_time"],
                            "payload": lap_payload,
                        },
                        priority=3,
                    )
                last_completed_lap = completed_lap
        except KeyboardInterrupt:
            print("\nTelemetry analyzer stopped.")
            break
        except Exception as exc:
            print(f"[Analyzer] Error: {exc}")


if __name__ == "__main__":
    main()
