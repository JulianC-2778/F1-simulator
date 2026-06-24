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
ENABLE_TTS = env_flag("TORCS_ANALYZER_TTS", False)
ENABLE_RULE_FASTPATH = env_flag("TORCS_ANALYZER_RULE_FASTPATH", True)
TTS_VOICE = os.getenv("TORCS_ANALYZER_TTS_VOICE", "en-us")
TTS_RATE = int(os.getenv("TORCS_ANALYZER_TTS_RATE", "160"))
PULSE_SERVER = os.getenv("TORCS_ANALYZER_PULSE_SERVER", "/mnt/wslg/PulseServer")
MODEL_BASE_URL = os.getenv("TORCS_ANALYZER_BASE_URL", DEFAULT_MODEL_BASE_URL)
MODEL_NAME = os.getenv("TORCS_ANALYZER_MODEL", DEFAULT_MODEL_NAME)


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

    pit_advice = "No pit stop needed yet."
    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        pit_advice = "Pit now. Fuel or damage is already in the danger zone."
    elif latest["fuel"] < 10.0 or latest["damage"] > 25.0:
        pit_advice = "Pit soon. Fuel or damage is trending risky."

    if opponent_profile["front_gap"] < 8.0 and latest["speed_x"] > 60.0:
        return {
            "headline": "Traffic Alert",
            "focus_area": "traffic",
            "priority": "high",
            "analysis": f"Front gap is only {opponent_profile['front_gap']:.1f} m, so the overtake or braking window is tight.",
            "action": "Defend the inside and brake slightly earlier to avoid contact with the car ahead.",
            "pit_advice": pit_advice,
            "confidence": 0.95,
            "fast_path": True,
        }

    if abs(latest["track_pos"]) > 1.0:
        side = "left" if latest["track_pos"] < 0 else "right"
        return {
            "headline": "Off Track Risk",
            "focus_area": "cornering",
            "priority": "high",
            "analysis": f"The car is already beyond the {side} track edge with track position {latest['track_pos']:.2f}.",
            "action": "Straighten the steering, ease off the throttle, and rejoin the track smoothly before pushing again.",
            "pit_advice": pit_advice,
            "confidence": 0.98,
            "fast_path": True,
        }

    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        return {
            "headline": "Pit Window Open",
            "focus_area": "pit_strategy",
            "priority": "high",
            "analysis": "Fuel or damage has crossed the local safety threshold.",
            "action": "Commit to a pit stop at the next safe opportunity.",
            "pit_advice": pit_advice,
            "confidence": 0.97,
            "fast_path": True,
        }

    if track_profile["center_opening"] < 25.0 and latest["brake"] < 0.08 and latest["speed_x"] > 90.0:
        return {
            "headline": "Braking Point",
            "focus_area": "braking",
            "priority": "medium",
            "analysis": "The road ahead is tightening, but brake pressure is still very low for the current speed.",
            "action": "Brake earlier for the next corner and finish most of the braking before turn-in.",
            "pit_advice": pit_advice,
            "confidence": 0.82,
            "fast_path": False,
        }

    if summary.get("steering_stddev", 0.0) > 0.35 and abs(latest["track_pos"]) > 0.6:
        return {
            "headline": "Line Stability",
            "focus_area": "cornering",
            "priority": "medium",
            "analysis": "Steering corrections are high while the car is already close to the edge of the track.",
            "action": "Use one smoother steering input and let the car breathe back toward the center on exit.",
            "pit_advice": pit_advice,
            "confidence": 0.8,
            "fast_path": False,
        }

    if summary.get("avg_throttle", 0.0) < 0.35 and track_profile["center_opening"] > 60.0 and latest["speed_x"] > 70.0:
        return {
            "headline": "Throttle Timing",
            "focus_area": "throttle",
            "priority": "medium",
            "analysis": "There is open road ahead, but throttle application over the last window has stayed conservative.",
            "action": "Start squeezing on the throttle earlier once the steering wheel begins to unwind.",
            "pit_advice": pit_advice,
            "confidence": 0.76,
            "fast_path": False,
        }

    return {
        "headline": "Rhythm Check",
        "focus_area": "cornering",
        "priority": "low",
        "analysis": "The current window looks stable, with no urgent danger signal from the local rules.",
        "action": "Keep building rhythm and focus on a clean entry-to-exit line.",
        "pit_advice": pit_advice,
        "confidence": 0.62,
        "fast_path": False,
    }


def build_live_payload(frames: list[dict[str, Any]], rule_feedback: dict[str, Any]) -> dict[str, Any]:
    latest = frames[-1]
    summary = summarize_frames(frames)
    return {
        "task": "telemetry_coaching",
        "analysis_type": "live_window",
        "window_seconds": LIVE_WINDOW,
        "latest_state": latest_state_payload(latest),
        "window_summary": compact_live_summary(summary),
        "track_profile": compact_track_profile(latest["track"]),
        "opponent_profile": compact_opponent_profile(latest["opponents"]),
        "rule_hint": {
            "focus_area": rule_feedback["focus_area"],
            "priority": rule_feedback["priority"],
            "action": rule_feedback["action"],
            "pit_advice": rule_feedback["pit_advice"],
        },
        "objectives": [
            "Give one short, actionable instruction.",
            "If the local rule hint is strong, align with it unless telemetry strongly suggests otherwise.",
            "Keep the response useful for real-time coaching.",
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
        "latest_state": latest_state_payload(latest),
        "lap_summary": compact_lap_summary(summary),
        "track_profile": compact_track_profile(latest["track"]),
        "opponent_profile": compact_opponent_profile(latest["opponents"]),
        "rule_hint": {
            "focus_area": rule_feedback["focus_area"],
            "action": rule_feedback["action"],
            "pit_advice": rule_feedback["pit_advice"],
        },
        "objectives": [
            "Summarize pace in one sentence.",
            "Give one braking tip, one cornering tip, and one throttle tip.",
            "State clearly whether the driver should think about pitting soon.",
        ],
    }


def call_model(connection: Any, payload: dict[str, Any]) -> dict[str, Any]:
    prompt = f"""You are an F1-style racing engineer for a TORCS simulator student project.
Read the structured telemetry payload and respond with JSON only.

Required JSON schema:
{{
  "headline": "short title",
  "focus_area": "braking|cornering|throttle|traffic|pit_strategy",
  "priority": "low|medium|high",
  "analysis": "1-2 sentence explanation",
  "action": "one specific driver instruction",
  "pit_advice": "clear answer about pit now / pit later / no pit",
  "confidence": 0.0
}}

Rules:
- Be concise and concrete.
- Keep live-window actions short enough to say over radio.
- Respect the local rule hint unless there is strong evidence to disagree.
- For lap reviews, mention braking, cornering line, and throttle timing.

Telemetry payload:
{json.dumps(payload, ensure_ascii=True)}"""

    text = chat_completion_text(
        connection,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=220,
        timeout=12.0,
    )
    parsed = extract_json_object(text)
    if parsed is not None:
        return parsed
    return {
        "headline": "Model response",
        "focus_area": "cornering",
        "priority": "medium",
        "analysis": text,
        "action": text,
        "pit_advice": "Unable to parse structured pit advice.",
        "confidence": 0.3,
    }


def print_analysis(prefix: str, result: dict[str, Any]) -> None:
    print(f"\n[{prefix}] {result.get('headline', 'Analysis')}")
    print(f"  Focus: {result.get('focus_area', 'unknown')} | Priority: {result.get('priority', 'unknown')}")
    print(f"  Why: {result.get('analysis', '')}")
    print(f"  Do: {result.get('action', '')}")
    print(f"  Pit: {result.get('pit_advice', '')}")


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
    if current_sim_time - requested_sim_time > MAX_STALE_SECONDS:
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
                    live_payload = build_live_payload(recent_frames, rule_feedback)
                    live_worker.submit(
                        {
                            "label": f"LIVE {recent_frames[0]['sim_time']:.1f}s-{latest['sim_time']:.1f}s",
                            "requested_sim_time": latest["sim_time"],
                            "payload": live_payload,
                        },
                        priority=2 if rule_feedback["priority"] == "high" else 1,
                    )

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
