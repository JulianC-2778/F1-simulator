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
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

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
    priority_issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
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
    }


def overlay_prompt(payload: dict[str, Any]) -> str:
    return f"""Add a short model supplement for this fixed TORCS coaching card.
Return one valid JSON object only. English only.
Do not change the fixed radio_cue, action, priority_issues, pit_advice, or targets.
Keep it fast: analysis <= 32 words, coach_note <= 18 words, each tip <= 14 words.
Use only telemetry values present in the payload.

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

    common_frames = [to_common_frame(frame) for frame in raw_frames]
    if not common_frames:
        return empty_dashboard(window_seconds, history_seconds)

    live_frames = select_recent_frames(common_frames, window_seconds) or common_frames
    history_frames = select_recent_frames(common_frames, history_seconds) or common_frames
    latest = live_frames[-1]
    summary = summarize_frames(live_frames)
    track_profile = compact_track_profile(latest["track"])
    opponent_profile = compact_opponent_profile(latest["opponents"])
    rule_feedback = build_rule_feedback(live_frames)
    priority_issues = build_priority_issues(latest, summary, track_profile, opponent_profile)
    radio_cue = build_radio_cue(rule_feedback)
    overlay_request = overlay_payload(
        latest,
        summary,
        track_profile,
        opponent_profile,
        rule_feedback,
        priority_issues,
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
            "why": rule_feedback.get("why", ""),
            "immediate_steps": rule_feedback.get("immediate_steps", []),
            "next_lap_focus": rule_feedback.get("next_lap_focus", ""),
            "risk": rule_feedback.get("risk", ""),
            "target_metrics": rule_feedback.get("target_metrics", {}),
            "metrics": rule_feedback.get("metrics", {}),
            "priority_issues": priority_issues,
            "radio_cue": radio_cue,
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
