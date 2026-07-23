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
from midware.telemetry import to_common_frame


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


def empty_track_context(error: str = "") -> dict[str, Any]:
    return {
        "available": False,
        "source": "track_model",
        "name": "",
        "summary": "",
        "dist_from_start": 0.0,
        "limit_kmh": 0.0,
        "speed_over_limit": 0.0,
        "line_tpos": 0.0,
        "line_hint": "center",
        "next_corner": None,
        "error": error,
    }


def track_briefing(track_context: dict[str, Any] | None) -> dict[str, Any]:
    track_context = track_context or empty_track_context()
    if not track_context.get("available"):
        return {
            "status": "sensor_only",
            "brief": "Track map unavailable; guidance is using live sensors only.",
            "items": [
                {"label": "Map mode", "value": "sensor-only"},
                {"label": "Fallback", "value": "live telemetry"},
            ],
            "error": clean_text(track_context.get("error")),
        }

    next_corner = track_context.get("next_corner")
    if isinstance(next_corner, dict):
        corner_value = f"{clean_text(next_corner.get('dir') or 'corner')} in {safe_float(next_corner.get('dist_m')):.0f} m"
    else:
        corner_value = "none nearby"

    speed_over = safe_float(track_context.get("speed_over_limit"))
    speed_delta = f"+{speed_over:.0f} km/h" if speed_over > 0.0 else f"{speed_over:.0f} km/h"
    return {
        "status": "map_assist",
        "brief": (
            f"Map assist active on {clean_text(track_context.get('name') or 'current track')}: "
            f"limit {safe_float(track_context.get('limit_kmh')):.0f} km/h, {corner_value}, "
            f"line target {clean_text(track_context.get('line_hint') or 'center')}."
        ),
        "items": [
            {"label": "Map mode", "value": "loaded"},
            {"label": "Limit", "value": f"{safe_float(track_context.get('limit_kmh')):.0f} km/h"},
            {"label": "Speed delta", "value": speed_delta},
            {"label": "Next", "value": corner_value},
            {"label": "Line", "value": clean_text(track_context.get("line_hint") or "center")},
        ],
        "error": "",
    }


DRIVER_STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "balanced": {
        "label": "Balanced rhythm",
        "speed_margin_kmh": 0.0,
        "brake_margin_m": 0.0,
        "traits": ["repeatable references", "neutral risk appetite"],
        "risk_bias": "Minor mistakes usually come from changing too many inputs at once.",
        "setup": "Keep the baseline and improve one corner at a time.",
    },
    "late_braker": {
        "label": "Late braker",
        "speed_margin_kmh": 8.0,
        "brake_margin_m": 35.0,
        "traits": ["compresses braking zones", "needs earlier map markers"],
        "risk_bias": "Entry overspeed and understeer are the first risks to manage.",
        "setup": "Move the first brake reference earlier than instinct, then release pressure before turn-in.",
    },
    "edge_pusher": {
        "label": "Edge pusher",
        "speed_margin_kmh": 5.0,
        "brake_margin_m": 18.0,
        "traits": ["uses full road", "higher track-limit exposure"],
        "risk_bias": "Track-limit corrections can cost more time than a calmer entry.",
        "setup": "Leave half a car-width margin on entry and use the exit road only once the car is settled.",
    },
    "throttle_hesitant": {
        "label": "Throttle hesitant",
        "speed_margin_kmh": -2.0,
        "brake_margin_m": 0.0,
        "traits": ["protects exits", "leaves acceleration margin"],
        "risk_bias": "Lost time compounds on straights after medium-speed corners.",
        "setup": "Plan an earlier progressive throttle ramp as soon as steering begins to unwind.",
    },
    "reactive_steering": {
        "label": "Reactive steering",
        "speed_margin_kmh": 6.0,
        "brake_margin_m": 22.0,
        "traits": ["mid-corner corrections", "unstable entry line"],
        "risk_bias": "A second steering input near the edge can create understeer or a snap correction.",
        "setup": "Prioritise a settled outside entry and one steering arc through the apex.",
    },
    "aggressive": {
        "label": "Aggressive attack",
        "speed_margin_kmh": 4.0,
        "brake_margin_m": 15.0,
        "traits": ["accepts smaller margins", "fast commitment"],
        "risk_bias": "Aggressive entries need earlier escape margins in traffic or low grip.",
        "setup": "Keep the attack, but pre-commit to the safest exit before the braking zone.",
    },
    "conservative": {
        "label": "Conservative build-up",
        "speed_margin_kmh": -4.0,
        "brake_margin_m": -8.0,
        "traits": ["protects the car", "builds pace gradually"],
        "risk_bias": "The main time loss is usually delayed throttle rather than corner entry.",
        "setup": "Use the stable entry to open the wheel earlier and start power sooner.",
    },
}


ROAD_CONDITION_PRESETS: dict[str, dict[str, Any]] = {
    "dry": {
        "label": "Dry",
        "speed_margin_kmh": 0.0,
        "brake_margin_m": 0.0,
        "note": "Baseline grip is available; optimise references and repeatability.",
    },
    "low_grip": {
        "label": "Low grip",
        "speed_margin_kmh": 10.0,
        "brake_margin_m": 35.0,
        "note": "Reduce entry speed and avoid sharp throttle or steering changes.",
    },
    "wet": {
        "label": "Wet",
        "speed_margin_kmh": 18.0,
        "brake_margin_m": 55.0,
        "note": "Brake earlier, turn in softer, and delay full throttle until the car is straight.",
    },
    "traffic": {
        "label": "Traffic",
        "speed_margin_kmh": 5.0,
        "brake_margin_m": 25.0,
        "note": "Leave space for alternate lines and avoid late defensive moves before turn-in.",
    },
    "worn_tyres": {
        "label": "Worn tyres",
        "speed_margin_kmh": 12.0,
        "brake_margin_m": 40.0,
        "note": "Protect exits and avoid loading the front tyre with late steering corrections.",
    },
}


def driver_style_profile(frames: list[dict[str, Any]] | None = None, explicit_style: str | None = None) -> dict[str, Any]:
    style_id = clean_text(explicit_style).lower().replace("-", "_").replace(" ", "_")
    inferred = False
    summary: dict[str, Any] = {}

    if not style_id or style_id == "auto":
        inferred = True
        style_id = "balanced"
        if frames:
            summary = summarize_frames(frames)
            if summary.get("off_track_moments", 0) > 0 or summary.get("track_pos_stddev", 0.0) > 0.42:
                style_id = "edge_pusher"
            elif summary.get("steering_stddev", 0.0) > 0.35:
                style_id = "reactive_steering"
            elif summary.get("avg_throttle", 0.0) < 0.35 and summary.get("avg_speed", 0.0) > 70.0:
                style_id = "throttle_hesitant"
            elif summary.get("avg_brake", 0.0) < 0.10 and summary.get("avg_speed", 0.0) > 95.0:
                style_id = "late_braker"

    preset = DRIVER_STYLE_PRESETS.get(style_id, DRIVER_STYLE_PRESETS["balanced"])
    return {
        "id": style_id if style_id in DRIVER_STYLE_PRESETS else "balanced",
        "label": preset["label"],
        "source": "inferred" if inferred else "selected",
        "confidence": 0.72 if inferred and frames else 0.95 if not inferred else 0.50,
        "speed_margin_kmh": preset["speed_margin_kmh"],
        "brake_margin_m": preset["brake_margin_m"],
        "traits": list(preset["traits"]),
        "risk_bias": preset["risk_bias"],
        "setup": preset["setup"],
        "evidence": {
            "avg_throttle": round(summary.get("avg_throttle", 0.0), 3),
            "avg_brake": round(summary.get("avg_brake", 0.0), 3),
            "steering_stddev": round(summary.get("steering_stddev", 0.0), 3),
            "track_pos_stddev": round(summary.get("track_pos_stddev", 0.0), 3),
            "off_track_moments": int(summary.get("off_track_moments", 0)),
        },
    }


def road_condition_profile(value: str | None = None) -> dict[str, Any]:
    condition_id = clean_text(value).lower().replace("-", "_").replace(" ", "_") or "dry"
    preset = ROAD_CONDITION_PRESETS.get(condition_id, ROAD_CONDITION_PRESETS["dry"])
    return {
        "id": condition_id if condition_id in ROAD_CONDITION_PRESETS else "dry",
        "label": preset["label"],
        "speed_margin_kmh": preset["speed_margin_kmh"],
        "brake_margin_m": preset["brake_margin_m"],
        "note": preset["note"],
    }


def build_lookahead_plan(
    track_context: dict[str, Any] | None,
    driver_profile: dict[str, Any] | None = None,
    road_condition: dict[str, Any] | None = None,
    *,
    horizon: int = 4,
) -> list[dict[str, Any]]:
    track_context = track_context or empty_track_context()
    driver_profile = driver_profile or driver_style_profile([])
    road_condition = road_condition or road_condition_profile("dry")
    if not track_context.get("available"):
        return []

    corners = track_context.get("upcoming_corners") or []
    if not isinstance(corners, list):
        corners = []
    if not corners and isinstance(track_context.get("next_corner"), dict):
        corners = [track_context["next_corner"]]

    speed_margin = safe_float(driver_profile.get("speed_margin_kmh")) + safe_float(road_condition.get("speed_margin_kmh"))
    brake_margin = safe_float(driver_profile.get("brake_margin_m")) + safe_float(road_condition.get("brake_margin_m"))
    output: list[dict[str, Any]] = []
    for index, corner in enumerate(corners[: max(1, horizon)]):
        limit = safe_float(corner.get("limit_kmh"), safe_float(track_context.get("limit_kmh"), 120.0))
        target_speed = max(45.0, limit - speed_margin)
        brake_in = max(0.0, safe_float(corner.get("brake_in_m"), safe_float(corner.get("dist_m"))) - brake_margin)
        direction = clean_text(corner.get("dir") or "corner")
        line_hint = clean_text(corner.get("line_hint") or track_context.get("line_hint") or "outside")
        priority = "high" if index == 0 and brake_in < 280.0 else "medium" if index < 2 else "low"
        output.append(
            {
                "id": clean_text(corner.get("id")) or f"corner-{index + 1}",
                "name": clean_text(corner.get("name")) or f"Upcoming {direction} corner",
                "dir": direction,
                "priority": priority,
                "distance_m": round(safe_float(corner.get("dist_m")), 1),
                "brake_in_m": round(brake_in, 1),
                "target_speed_kmh": round(target_speed, 1),
                "line_hint": line_hint,
                "risk": clean_text(corner.get("risk")) or clean_text(driver_profile.get("risk_bias")),
                "action": (
                    f"Prepare {line_hint}; target {target_speed:.0f} km/h before the {direction} corner "
                    f"and add the brake margin early."
                ),
                "driver_adjustment": clean_text(driver_profile.get("setup")),
                "road_adjustment": clean_text(road_condition.get("note")),
            }
        )
    return output


def build_pre_race_briefing(
    track_context: dict[str, Any] | None,
    driver_profile: dict[str, Any] | None = None,
    road_condition: dict[str, Any] | None = None,
    lookahead_plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    track_context = track_context or empty_track_context()
    driver_profile = driver_profile or driver_style_profile([])
    road_condition = road_condition or road_condition_profile("dry")
    lookahead_plan = lookahead_plan if lookahead_plan is not None else build_lookahead_plan(track_context, driver_profile, road_condition)

    if not track_context.get("available"):
        return {
            "status": "sensor_only",
            "source": "rule_engine",
            "headline": "Pre-race map unavailable",
            "summary": "Live telemetry guidance remains available, but pre-race prediction needs a loaded track profile.",
            "radio_brief": "Track map missing. Build the first lap carefully and let live telemetry establish references.",
            "driver_profile": driver_profile,
            "road_condition": road_condition,
            "strategy_stack": [],
            "risk_stack": ["Track profile is not loaded."],
            "lookahead_plan": [],
            "model_supplement": {"status": "not_requested", "text": "", "error": ""},
        }

    first = lookahead_plan[0] if lookahead_plan else {}
    track_name = clean_text(track_context.get("name") or "current track")
    headline = f"{track_name}: {driver_profile['label']} pre-race plan"
    first_action = clean_text(first.get("action")) or "Use the opening lap to confirm grip and braking references."
    strategy_stack = [
        clean_text(driver_profile.get("setup")),
        clean_text(road_condition.get("note")),
        first_action,
    ]
    risk_stack = [
        clean_text(driver_profile.get("risk_bias")),
        clean_text(first.get("risk")) if first else "No major mapped corner risk in the current lookahead.",
    ]
    return {
        "status": "ready",
        "source": "rule_engine",
        "headline": headline,
        "summary": (
            f"{clean_text(track_context.get('summary'))} Road condition: {road_condition['label']}. "
            f"Driver style: {driver_profile['label']}."
        ),
        "radio_brief": (
            f"{driver_profile['label']}: {first_action} "
            f"{road_condition['note']}"
        ),
        "driver_profile": driver_profile,
        "road_condition": road_condition,
        "strategy_stack": [item for item in strategy_stack if item][:3],
        "risk_stack": [item for item in risk_stack if item][:3],
        "lookahead_plan": lookahead_plan,
        "model_supplement": {"status": "not_requested", "text": "", "error": ""},
    }


def prebrief_prompt(payload: dict[str, Any]) -> str:
    return f"""You are a concise TORCS race coach.
Return one JSON object only with:
{{
  "brief": "one radio-style pre-race briefing, <= 55 words",
  "focus": ["3 short focus points"],
  "risk": "one main risk, <= 18 words"
}}

Use the rule forecast exactly; do not invent new track segments.
Payload:
{json.dumps(payload, ensure_ascii=True)}"""


def feedback(
    *,
    state_id: str,
    headline: str,
    focus_area: str,
    priority: str,
    analysis: str,
    action: str,
    pit_advice: str,
    confidence: float,
    why: str,
    immediate_steps: list[str],
    next_lap_focus: str,
    risk: str,
    target_metrics: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "state_id": state_id,
        "headline": headline,
        "focus_area": focus_area,
        "priority": priority,
        "analysis": analysis,
        "action": action,
        "pit_advice": pit_advice,
        "confidence": confidence,
        "why": why,
        "immediate_steps": immediate_steps[:3],
        "next_lap_focus": next_lap_focus,
        "risk": risk,
        "target_metrics": target_metrics,
        "metrics": metrics,
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


def issue(
    *,
    label: str,
    area: str,
    severity: str,
    evidence: str,
    correction: str,
) -> dict[str, str]:
    return {
        "label": label,
        "area": area,
        "severity": severity,
        "evidence": evidence,
        "correction": correction,
    }


def severity_rank(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


def build_priority_issues(
    latest: dict[str, Any],
    summary: dict[str, Any],
    track_profile: dict[str, Any],
    opponent_profile: dict[str, Any],
    track_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    track_context = track_context or empty_track_context()

    next_corner = track_context.get("next_corner") if track_context.get("available") else None
    if isinstance(next_corner, dict):
        corner_dist = safe_float(next_corner.get("dist_m"), 9999.0)
        map_limit = safe_float(track_context.get("limit_kmh"))
        speed_over = safe_float(track_context.get("speed_over_limit"))
        if corner_dist <= 280.0 and speed_over > 12.0:
            direction = clean_text(next_corner.get("dir") or "corner")
            issues.append(
                issue(
                    label="Mapped braking target",
                    area="braking",
                    severity="high" if speed_over > 28.0 else "medium",
                    evidence=f"{corner_dist:.0f} m to {direction} corner, map limit {map_limit:.0f} km/h, speed +{speed_over:.0f}",
                    correction="Use the map marker as an earlier brake reference before the sensor view tightens.",
                )
            )

    if track_context.get("available") and abs(safe_float(track_context.get("line_tpos"))) > 0.15:
        line_hint = clean_text(track_context.get("line_hint") or "outside")
        line_tpos = safe_float(track_context.get("line_tpos"))
        issues.append(
            issue(
                label="Pre-corner line setup",
                area="cornering",
                severity="low",
                evidence=f"Map line target {line_tpos:+.2f} ({line_hint})",
                correction="Blend toward the suggested side early, then let live sensor spacing decide the final apex.",
            )
        )

    if opponent_profile["front_gap"] < 8.0 and latest["speed_x"] > 60.0:
        issues.append(
            issue(
                label="Front gap compressed",
                area="traffic",
                severity="high",
                evidence=f"{opponent_profile['front_gap']:.1f} m ahead at {latest['speed_x']:.0f} km/h",
                correction="Brake earlier and avoid a second move before turn-in.",
            )
        )

    if abs(latest["track_pos"]) > 1.0:
        issues.append(
            issue(
                label="Track limits exceeded",
                area="cornering",
                severity="high",
                evidence=f"Track position {latest['track_pos']:+.2f}",
                correction="Straighten first, then rejoin before applying full throttle.",
            )
        )
    elif abs(latest["track_pos"]) > 0.8:
        issues.append(
            issue(
                label="Edge pressure",
                area="cornering",
                severity="medium",
                evidence=f"Track position {latest['track_pos']:+.2f}",
                correction="Leave half a car-width more margin on the next entry.",
            )
        )

    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        issues.append(
            issue(
                label="Pit threshold crossed",
                area="pit_strategy",
                severity="high",
                evidence=f"Fuel {latest['fuel']:.1f} L, damage {latest['damage']:.1f}",
                correction="Box at the next safe pit entry and stop fighting marginal moves.",
            )
        )
    elif latest["fuel"] < 10.0 or latest["damage"] > 25.0:
        issues.append(
            issue(
                label="Strategy margin shrinking",
                area="pit_strategy",
                severity="medium",
                evidence=f"Fuel {latest['fuel']:.1f} L, damage {latest['damage']:.1f}",
                correction="Prepare the next pit window and avoid kerb damage.",
            )
        )

    if track_profile["center_opening"] < 25.0 and latest["brake"] < 0.08 and latest["speed_x"] > 90.0:
        issues.append(
            issue(
                label="Late brake risk",
                area="braking",
                severity="medium",
                evidence=f"{latest['speed_x']:.0f} km/h, brake {latest['brake']:.2f}, opening {track_profile['center_opening']:.1f} m",
                correction="Move the brake marker earlier and release as steering builds.",
            )
        )

    if summary.get("steering_stddev", 0.0) > 0.35 and abs(latest["track_pos"]) > 0.6:
        issues.append(
            issue(
                label="Reactive steering",
                area="cornering",
                severity="medium",
                evidence=f"Steering variation {summary.get('steering_stddev', 0.0):.2f}",
                correction="Commit to one arc and delay throttle until the wheel opens.",
            )
        )

    if summary.get("avg_throttle", 0.0) < 0.35 and track_profile["center_opening"] > 60.0 and latest["speed_x"] > 70.0:
        issues.append(
            issue(
                label="Exit throttle left unused",
                area="throttle",
                severity="medium",
                evidence=f"Average throttle {summary.get('avg_throttle', 0.0):.2f}, opening {track_profile['center_opening']:.1f} m",
                correction="Start power at 30-40% as soon as steering unwinds.",
            )
        )

    if not issues:
        issues.append(
            issue(
                label="Baseline rhythm",
                area="consistency",
                severity="low",
                evidence=f"Avg speed {summary.get('avg_speed', 0.0):.0f} km/h, steering variation {summary.get('steering_stddev', 0.0):.2f}",
                correction="Improve one reference point instead of changing every input.",
            )
        )

    return sorted(issues, key=lambda item: severity_rank(item["severity"]))[:3]


def build_radio_cue(rule_feedback: dict[str, Any]) -> str:
    focus = clean_text(rule_feedback.get("focus_area") or "pace")
    correction = clean_text(rule_feedback.get("action"))
    if not correction:
        return "Hold the rhythm and wait for the next telemetry window."
    return f"{focus.title()}: {correction}"


def build_rule_feedback(frames: list[dict[str, Any]], track_context: dict[str, Any] | None = None) -> dict[str, Any]:
    latest = frames[-1]
    summary = summarize_frames(frames)
    track_profile = compact_track_profile(latest["track"])
    opponent_profile = compact_opponent_profile(latest["opponents"])
    track_context = track_context or empty_track_context()

    pit_advice = "No pit stop needed yet."
    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        pit_advice = "Pit now. Fuel or damage is already in the danger zone."
    elif latest["fuel"] < 10.0 or latest["damage"] > 25.0:
        pit_advice = "Pit soon. Fuel or damage is trending risky."

    if opponent_profile["front_gap"] < 8.0 and latest["speed_x"] > 60.0:
        return feedback(
            state_id="collision_risk",
            headline="Traffic Alert",
            focus_area="traffic",
            priority="high",
            analysis=f"Front gap is {opponent_profile['front_gap']:.1f} m at {latest['speed_x']:.0f} km/h, so the braking and overtake window is compressed.",
            action="Brake a touch earlier, hold one predictable line, and avoid diving into the car ahead.",
            pit_advice=pit_advice,
            confidence=0.95,
            why="At this gap, late braking or a second steering correction can turn a normal battle into contact.",
            immediate_steps=[
                "Move once to cover or follow; do not weave.",
                "Lift or brake 0.2-0.4 s earlier than the previous marker.",
                "Keep the steering settled until the front gap opens above 12 m.",
            ],
            next_lap_focus="Use the same braking marker once, then compare whether the front gap grows on corner exit.",
            risk="High contact risk if throttle stays high into the braking zone.",
            target_metrics={"front_gap_min_m": 12.0, "avg_brake_min": 0.18, "steering_stddev_max": 0.30},
            metrics={
                "front_gap": opponent_profile["front_gap"],
                "nearest_gap": opponent_profile["nearest_gap"],
                "speed_x": latest["speed_x"],
                "avg_brake": summary.get("avg_brake", 0.0),
            },
        )

    if abs(latest["track_pos"]) > 1.0:
        side = "left" if latest["track_pos"] < 0 else "right"
        return feedback(
            state_id="off_track_recovery",
            headline="Rejoin Cleanly",
            focus_area="cornering",
            priority="high",
            analysis=f"The car is beyond the {side} edge with track position {latest['track_pos']:.2f}, so grip and steering response are unreliable.",
            action="Straighten the wheel, reduce throttle, and rejoin gradually before asking for full power.",
            pit_advice=pit_advice,
            confidence=0.98,
            why="The fastest recovery is a stable rejoin; aggressive steering while off track usually creates a spin or damage spike.",
            immediate_steps=[
                "Hold the wheel closer to center until the car is back inside the limits.",
                "Keep throttle below 40% during the rejoin.",
                "Wait one clean car length before accelerating hard again.",
            ],
            next_lap_focus="Reduce corner entry speed slightly and aim to keep track position inside +/-0.75 through the same section.",
            risk="Spin or wall contact if throttle rises before the car is straight.",
            target_metrics={"track_pos_abs_max": 0.75, "throttle_rejoin_max": 0.40, "damage_delta_max": 0.0},
            metrics={
                "track_pos": latest["track_pos"],
                "speed_x": latest["speed_x"],
                "damage_delta": summary.get("damage_delta", 0.0),
                "off_track_moments": summary.get("off_track_moments", 0),
            },
        )

    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        return feedback(
            state_id="pit_now",
            headline="Box This Lap",
            focus_area="pit_strategy",
            priority="high",
            analysis=f"Fuel is {latest['fuel']:.1f} L and damage is {latest['damage']:.1f}, which crosses the local safety threshold.",
            action="Commit to the pit entry; avoid extra fights and protect the car until the stop.",
            pit_advice=pit_advice,
            confidence=0.97,
            why="Continuing to push with this fuel or damage level risks losing more time than the pit stop costs.",
            immediate_steps=[
                "Confirm pit entry line early and avoid late defensive moves.",
                "Short-shift and stay off kerbs to reduce damage risk.",
                "Give up a marginal battle if it compromises pit entry.",
            ],
            next_lap_focus="After the stop, rebuild rhythm for one lap before attacking.",
            risk="Running out of fuel margin or accumulating race-ending damage.",
            target_metrics={"fuel_min_l": 6.0, "damage_max": 40.0, "extra_damage_max": 0.0},
            metrics={"fuel": latest["fuel"], "damage": latest["damage"], "damage_delta": summary.get("damage_delta", 0.0)},
        )

    next_corner = track_context.get("next_corner") if track_context.get("available") else None
    if isinstance(next_corner, dict):
        corner_dist = safe_float(next_corner.get("dist_m"), 9999.0)
        map_limit = safe_float(track_context.get("limit_kmh"))
        speed_over = safe_float(track_context.get("speed_over_limit"))
        line_tpos = safe_float(track_context.get("line_tpos"))
        if corner_dist <= 320.0 and speed_over > 14.0 and latest["brake"] < 0.40:
            direction = clean_text(next_corner.get("dir") or "corner")
            return feedback(
                state_id="mapped_braking_setup",
                headline="Brake To The Map",
                focus_area="braking",
                priority="high" if speed_over > 28.0 else "medium",
                analysis=(
                    f"The track map shows a {direction} corner in {corner_dist:.0f} m with a {map_limit:.0f} km/h target, "
                    f"while current speed is {latest['speed_x']:.0f} km/h."
                ),
                action="Start the braking phase from the map marker, then release progressively as the live track sensors confirm the corner.",
                pit_advice=pit_advice,
                confidence=0.84,
                why="The preloaded map can see beyond the short sensor window, so it is useful for avoiding late braking before blind or fast-approach corners.",
                immediate_steps=[
                    f"Reduce speed toward {map_limit:.0f} km/h before the corner entry.",
                    "Use one firm initial brake input, then bleed pressure before turn-in.",
                    f"Prepare the car toward the {track_context.get('line_hint') or 'outside'} before the entry.",
                ],
                next_lap_focus="Compare whether the same map marker produces a cleaner entry and earlier exit throttle.",
                risk="Arriving above the map target can force understeer, a missed apex, or a track-limit correction.",
                target_metrics={
                    "map_limit_kmh": round(map_limit, 1),
                    "speed_over_limit_max": 8.0,
                    "corner_distance_m": round(corner_dist, 1),
                },
                metrics={
                    "speed_x": latest["speed_x"],
                    "map_limit_kmh": map_limit,
                    "speed_over_limit": speed_over,
                    "next_corner_distance": corner_dist,
                    "line_tpos": line_tpos,
                },
            )

        if corner_dist <= 260.0 and abs(line_tpos) > 0.15 and abs(latest["track_pos"] - line_tpos) > 0.35:
            return feedback(
                state_id="mapped_entry_line",
                headline="Set The Entry Line",
                focus_area="cornering",
                priority="medium",
                analysis=(
                    f"The map suggests a {track_context.get('line_hint') or 'side'} setup before the next corner, "
                    f"but current track position is {latest['track_pos']:+.2f} versus target {line_tpos:+.2f}."
                ),
                action="Blend toward the map line early, then let live traffic and track sensors decide the final apex.",
                pit_advice=pit_advice,
                confidence=0.74,
                why="A stable outside entry gives more room to rotate the car and reduces mid-corner steering corrections.",
                immediate_steps=[
                    f"Move gradually toward track position {line_tpos:+.2f}.",
                    "Avoid a late lateral snap just before braking.",
                    "Hold the setup line until the corner is inside the live sensor window.",
                ],
                next_lap_focus="Check whether the earlier line setup reduces steering variation through the same corner.",
                risk="A late line change can scrub speed or create a track-limit risk on turn-in.",
                target_metrics={
                    "line_target_tpos": round(line_tpos, 2),
                    "track_pos_error_max": 0.25,
                    "steering_stddev_max": 0.30,
                },
                metrics={
                    "track_pos": latest["track_pos"],
                    "line_tpos": line_tpos,
                    "track_pos_error": abs(latest["track_pos"] - line_tpos),
                    "next_corner_distance": corner_dist,
                },
            )

    if track_profile["center_opening"] < 25.0 and latest["brake"] < 0.08 and latest["speed_x"] > 90.0:
        return feedback(
            state_id="late_braking",
            headline="Brake Earlier",
            focus_area="braking",
            priority="medium",
            analysis=f"Center track opening is only {track_profile['center_opening']:.1f} m while speed is {latest['speed_x']:.0f} km/h and brake input is {latest['brake']:.2f}.",
            action="Move the braking marker earlier and finish the heavy braking before turn-in.",
            pit_advice=pit_advice,
            confidence=0.82,
            why="The car is arriving too quickly for the available road, so late braking will force understeer or a wide exit.",
            immediate_steps=[
                "Begin braking before the road visually tightens.",
                "Use one firm initial brake input instead of trailing in too late.",
                "Release brake progressively as steering angle increases.",
            ],
            next_lap_focus="Compare minimum corner speed and exit track position; the target is a calmer entry with earlier throttle on exit.",
            risk="Overshooting the apex and losing exit speed.",
            target_metrics={"brake_now_min": 0.18, "center_opening_min_m": 25.0, "track_pos_abs_max": 0.80},
            metrics={
                "speed_x": latest["speed_x"],
                "brake": latest["brake"],
                "center_opening": track_profile["center_opening"],
                "front_track_clearance_now": summary.get("front_track_clearance_now", 0.0),
            },
        )

    if summary.get("steering_stddev", 0.0) > 0.35 and abs(latest["track_pos"]) > 0.6:
        return feedback(
            state_id="unstable_line",
            headline="Calm The Wheel",
            focus_area="cornering",
            priority="medium",
            analysis=f"Steering variation is {summary.get('steering_stddev', 0.0):.2f} while track position is {latest['track_pos']:.2f}, so the line is becoming reactive near the edge.",
            action="Commit to one steering arc and let the car drift back toward the middle before adding more throttle.",
            pit_advice=pit_advice,
            confidence=0.80,
            why="Multiple corrections near the edge usually mean the entry line was not settled early enough.",
            immediate_steps=[
                "Turn in once, then hold the steering angle for longer.",
                "Delay throttle until the wheel starts to open.",
                "Use the full road on exit but avoid crossing +/-0.85 track position.",
            ],
            next_lap_focus="Enter the same corner half a car-width wider and reduce mid-corner steering corrections.",
            risk="A snap correction can push the car beyond track limits or scrub speed.",
            target_metrics={"steering_stddev_max": 0.30, "track_pos_abs_max": 0.85, "avg_throttle_exit_min": 0.45},
            metrics={
                "steering_stddev": summary.get("steering_stddev", 0.0),
                "track_pos": latest["track_pos"],
                "track_pos_stddev": summary.get("track_pos_stddev", 0.0),
            },
        )

    if summary.get("avg_throttle", 0.0) < 0.35 and track_profile["center_opening"] > 60.0 and latest["speed_x"] > 70.0:
        return feedback(
            state_id="throttle_hesitation",
            headline="Earlier Throttle",
            focus_area="throttle",
            priority="medium",
            analysis=f"Average throttle is only {summary.get('avg_throttle', 0.0):.2f} with {track_profile['center_opening']:.1f} m of center opening, so exit acceleration is being left unused.",
            action="Start squeezing throttle earlier as soon as steering begins to unwind.",
            pit_advice=pit_advice,
            confidence=0.76,
            why="The road is open enough to build speed, but the throttle trace shows hesitation after the corner opens.",
            immediate_steps=[
                "Begin throttle at 30-40% before the wheel is fully straight.",
                "Add power progressively, not as one late punch.",
                "If the rear steps out, pause throttle rather than adding steering correction.",
            ],
            next_lap_focus="Aim for smoother throttle build-up over the same exit and compare speed delta on the following straight.",
            risk="Leaving exit speed on the table and losing time down the next straight.",
            target_metrics={"avg_throttle_min": 0.45, "center_opening_min_m": 60.0, "speed_delta_min": 5.0},
            metrics={
                "avg_throttle": summary.get("avg_throttle", 0.0),
                "center_opening": track_profile["center_opening"],
                "speed_delta": summary.get("speed_delta", 0.0),
            },
        )

    return feedback(
        state_id="stable_rhythm",
        headline="Build Rhythm",
        focus_area="cornering",
        priority="low",
        analysis=f"The window is stable: average speed is {summary.get('avg_speed', 0.0):.0f} km/h, steering variation is {summary.get('steering_stddev', 0.0):.2f}, and no urgent risk trigger fired.",
        action="Keep the current rhythm, then choose one corner to improve rather than changing every input.",
        pit_advice=pit_advice,
        confidence=0.62,
        why="The cleanest gains now come from repeatability: same brake marker, same turn-in, smoother throttle build-up.",
        immediate_steps=[
            "Pick one reference marker for the next corner.",
            "Hold a steady steering arc through mid-corner.",
            "Review whether exit throttle can start slightly earlier.",
        ],
        next_lap_focus="Keep this baseline and improve one measurable item: either brake consistency, steering smoothness, or exit throttle.",
        risk="Low immediate risk; over-driving from a stable state could create unnecessary mistakes.",
        target_metrics={"steering_stddev_max": 0.30, "track_pos_abs_max": 0.80, "avg_throttle_min": 0.40},
        metrics={
            "avg_speed": summary.get("avg_speed", 0.0),
            "steering_stddev": summary.get("steering_stddev", 0.0),
            "avg_throttle": summary.get("avg_throttle", 0.0),
            "nearest_opponent_now": summary.get("nearest_opponent_now", 200.0),
        },
    )


def series_points(frames: list[dict[str, Any]], key: str) -> list[dict[str, float]]:
    return [
        {
            "sim_time": round(safe_float(frame.get("sim_time")), 3),
            "value": round(safe_float(frame.get(key)), 3),
        }
        for frame in frames
    ]


def overlay_key(rule_feedback: dict[str, Any], track_context: dict[str, Any] | None = None) -> str:
    track_context = track_context or empty_track_context()
    next_corner = track_context.get("next_corner") if track_context.get("available") else None
    parts = [
        clean_text(rule_feedback.get("state_id") or "stable_rhythm"),
        clean_text(rule_feedback.get("focus_area")),
        clean_text(rule_feedback.get("priority")),
        clean_text(rule_feedback.get("action")),
        clean_text(rule_feedback.get("pit_advice")),
        clean_text(track_context.get("name")) if track_context.get("available") else "sensor_only",
        str(round(safe_float(track_context.get("limit_kmh")), 0)) if track_context.get("available") else "",
        str(round(safe_float(next_corner.get("dist_m")), 0)) if isinstance(next_corner, dict) else "",
    ]
    return "|".join(parts)


def overlay_payload(
    latest: dict[str, Any],
    summary: dict[str, Any],
    track_profile: dict[str, Any],
    opponent_profile: dict[str, Any],
    rule_feedback: dict[str, Any],
    priority_issues: list[dict[str, str]] | None = None,
    track_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    track_context = track_context or empty_track_context()
    return {
        "state_id": rule_feedback.get("state_id", "stable_rhythm"),
        "focus_area": rule_feedback.get("focus_area", "cornering"),
        "priority": rule_feedback.get("priority", "medium"),
        "headline": rule_feedback.get("headline", "Guidance"),
        "action": rule_feedback.get("action", ""),
        "pit_advice": rule_feedback.get("pit_advice", "No pit stop needed yet."),
        "rule_reason": rule_feedback.get("analysis", ""),
        "why": rule_feedback.get("why", ""),
        "immediate_steps": rule_feedback.get("immediate_steps", []),
        "next_lap_focus": rule_feedback.get("next_lap_focus", ""),
        "risk": rule_feedback.get("risk", ""),
        "priority_issues": priority_issues or [],
        "radio_cue": build_radio_cue(rule_feedback),
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
        "track_profile": {
            "center_opening": round(track_profile.get("center_opening", 0.0), 3),
            "left_opening": round(track_profile.get("left_opening", 0.0), 3),
            "right_opening": round(track_profile.get("right_opening", 0.0), 3),
        },
        "opponent_profile": {
            "front_gap": round(opponent_profile.get("front_gap", 200.0), 3),
            "left_gap": round(opponent_profile.get("left_gap", 200.0), 3),
            "rear_gap": round(opponent_profile.get("rear_gap", 200.0), 3),
        },
        "track_model": track_context,
    }


def overlay_prompt(payload: dict[str, Any]) -> str:
    return f"""Add a short model supplement for this fixed TORCS coaching card.
Return one valid JSON object only. English only.
Do not change the fixed radio_cue, action, priority_issues, pit_advice, targets, or track_model values.
Keep it fast: analysis <= 32 words, coach_note <= 18 words, each tip <= 14 words.
Use only telemetry values present in the payload. If track_model is available, mention it only when it changes braking or line choice.

Schema:
{{
  "analysis": "cause and consequence",
  "coach_note": "radio-style support",
  "braking_tip": "braking adjustment or empty string",
  "cornering_tip": "line adjustment or empty string",
  "throttle_tip": "throttle adjustment or empty string"
}}

Payload:
{json.dumps(payload, ensure_ascii=True)}"""


def pending_overlay() -> dict[str, Any]:
    return {
        "status": "pending",
        "source": "model_overlay",
        "analysis": "",
        "coach_note": "",
        "braking_tip": "",
        "cornering_tip": "",
        "throttle_tip": "",
        "updated_at": None,
        "error": "",
    }


def empty_dashboard(
    window_seconds: float,
    history_seconds: float,
    *,
    error: str = "",
    upstream_ok: bool = True,
    message: str = "Waiting for a new telemetry session.",
    session_state: str = "waiting",
    session_id: int | None = None,
    data_age_seconds: float | None = None,
    track_context: dict[str, Any] | None = None,
    driver_style: str | None = None,
    road_condition: str | None = None,
) -> dict[str, Any]:
    track_context = track_context or empty_track_context()
    driver_profile = driver_style_profile([], driver_style)
    road = road_condition_profile(road_condition or track_context.get("road_condition") or "dry")
    lookahead_plan = build_lookahead_plan(track_context, driver_profile, road)
    pre_race_briefing = build_pre_race_briefing(track_context, driver_profile, road, lookahead_plan)
    return {
        "status": {
            "has_telemetry": False,
            "window_seconds": window_seconds,
            "history_seconds": history_seconds,
            "frame_count": 0,
            "upstream_ok": upstream_ok,
            "error": error,
            "message": message,
            "session_state": session_state,
            "session_id": session_id,
            "data_age_seconds": data_age_seconds,
        },
        "latest_state": None,
        "window_summary": None,
        "track_profile": None,
        "track_model": track_context,
        "driver_profile": driver_profile,
        "road_condition": road,
        "lookahead_plan": lookahead_plan,
        "pre_race_briefing": pre_race_briefing,
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
    track_context: dict[str, Any] | None = None,
    driver_style: str | None = None,
    road_condition: str | None = None,
) -> dict[str, Any]:
    if not raw_frames:
        return empty_dashboard(
            window_seconds,
            history_seconds,
            track_context=track_context,
            driver_style=driver_style,
            road_condition=road_condition,
        )

    common_frames = [to_common_frame(frame) for frame in raw_frames]
    if not common_frames:
        return empty_dashboard(
            window_seconds,
            history_seconds,
            track_context=track_context,
            driver_style=driver_style,
            road_condition=road_condition,
        )

    live_frames = select_recent_frames(common_frames, window_seconds) or common_frames
    history_frames = select_recent_frames(common_frames, history_seconds) or common_frames
    latest = live_frames[-1]
    summary = summarize_frames(live_frames)
    track_profile = compact_track_profile(latest["track"])
    opponent_profile = compact_opponent_profile(latest["opponents"])
    track_context = track_context or empty_track_context()
    driver_profile = driver_style_profile(live_frames, driver_style)
    road = road_condition_profile(road_condition or track_context.get("road_condition") or "dry")
    lookahead_plan = build_lookahead_plan(track_context, driver_profile, road)
    pre_race_briefing = build_pre_race_briefing(track_context, driver_profile, road, lookahead_plan)
    rule_feedback = build_rule_feedback(live_frames, track_context)
    priority_issues = build_priority_issues(latest, summary, track_profile, opponent_profile, track_context)
    radio_cue = build_radio_cue(rule_feedback)
    overlay_request = overlay_payload(
        latest,
        summary,
        track_profile,
        opponent_profile,
        rule_feedback,
        priority_issues,
        track_context,
    )

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
    if track_context.get("available"):
        map_limit = safe_float(track_context.get("limit_kmh"))
        speed_over = safe_float(track_context.get("speed_over_limit"))
        signals.append(
            {
                "label": "Map Limit",
                "value": map_limit,
                "display": f"{map_limit:.0f} km/h",
                "tone": "danger" if speed_over > 28.0 else "warn" if speed_over > 8.0 else "good",
            }
        )
    else:
        signals.append(
            {
                "label": "Track Map",
                "value": 0,
                "display": "sensor-only",
                "tone": "warn" if clean_text(track_context.get("error")) else "good",
            }
        )

    return {
        "status": {
            "has_telemetry": True,
            "window_seconds": window_seconds,
            "history_seconds": history_seconds,
            "frame_count": len(history_frames),
            "latest_sim_time": round(latest["sim_time"], 3),
            "upstream_ok": True,
            "error": "",
            "message": "Telemetry live.",
            "session_state": "live",
            "session_id": safe_int(latest.get("_session_id"), 0),
            "data_age_seconds": 0.0,
        },
        "latest_state": latest_state_payload(latest),
        "window_summary": compact_live_summary(summary),
        "track_profile": track_profile,
        "track_model": track_context,
        "driver_profile": driver_profile,
        "road_condition": road,
        "lookahead_plan": lookahead_plan,
        "pre_race_briefing": pre_race_briefing,
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
            "why": rule_feedback.get("why", ""),
            "immediate_steps": rule_feedback.get("immediate_steps", []),
            "next_lap_focus": rule_feedback.get("next_lap_focus", ""),
            "risk": rule_feedback.get("risk", ""),
            "target_metrics": rule_feedback.get("target_metrics", {}),
            "metrics": rule_feedback.get("metrics", {}),
            "priority_issues": priority_issues,
            "radio_cue": radio_cue,
            "track_briefing": track_briefing(track_context),
            "lookahead_plan": lookahead_plan,
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
        "_overlay_cache_key": overlay_key(rule_feedback, track_context),
    }
