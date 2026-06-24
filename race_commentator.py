#!/usr/bin/env python3
"""
Feature 3: Granite-powered procedural race commentary.
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
    latest_state_payload,
    normalize_text_key,
    print_connection_banner,
    select_recent_frames,
    speak_text,
    summarize_frames,
)


UDP_PORT = int(os.getenv("TORCS_COMMENTATOR_UDP_PORT", "3101"))
RETENTION_SECONDS = float(os.getenv("TORCS_COMMENTATOR_RETENTION_SECONDS", "180"))
BASELINE_INTERVAL = float(os.getenv("TORCS_COMMENTATOR_INTERVAL", "7.0"))
WINDOW_SECONDS = float(os.getenv("TORCS_COMMENTATOR_WINDOW_SECONDS", "6.0"))
LOOP_INTERVAL = float(os.getenv("TORCS_COMMENTATOR_LOOP_INTERVAL", "0.4"))
EVENT_COOLDOWN = float(os.getenv("TORCS_COMMENTATOR_EVENT_COOLDOWN", "1.0"))
MAX_STALE_SECONDS = float(os.getenv("TORCS_COMMENTATOR_MAX_STALE_SECONDS", "2.0"))
COMMENTARY_DEDUPE_SECONDS = float(os.getenv("TORCS_COMMENTATOR_DEDUPE_SECONDS", "10.0"))
ENABLE_TTS = env_flag("TORCS_COMMENTATOR_TTS", False)
TTS_VOICE = os.getenv("TORCS_COMMENTATOR_TTS_VOICE", "en-us")
TTS_RATE = int(os.getenv("TORCS_COMMENTATOR_TTS_RATE", "165"))
PULSE_SERVER = os.getenv("TORCS_COMMENTATOR_PULSE_SERVER", "/mnt/wslg/PulseServer")
MODEL_BASE_URL = os.getenv("TORCS_COMMENTATOR_BASE_URL", DEFAULT_MODEL_BASE_URL)
MODEL_NAME = os.getenv("TORCS_COMMENTATOR_MODEL", DEFAULT_MODEL_NAME)


EVENT_PRIORITIES = {
    "contact": 5,
    "position_change": 5,
    "off_track": 5,
    "lap_complete": 4,
    "battle": 4,
    "pace_surge": 3,
    "pace_update": 1,
}

EVENT_COOLDOWNS = {
    "contact": 1.0,
    "position_change": 1.0,
    "off_track": 1.2,
    "lap_complete": 1.0,
    "battle": 2.5,
    "pace_surge": 2.5,
    "pace_update": 6.0,
}


def compact_state_for_commentary(frame: dict[str, Any]) -> dict[str, Any]:
    state = latest_state_payload(frame)
    return {
        "sim_time": state["sim_time"],
        "lap": state["lap"],
        "speed_x": state["speed_x"],
        "gear": state["gear"],
        "track_pos": state["track_pos"],
        "damage": state["damage"],
        "race_pos": state["race_pos"],
        "fuel": state["fuel"],
    }


def event_signature(event: dict[str, Any]) -> str:
    return normalize_text_key(f"{event['event_type']} {event['reason']}", max_words=16)


def detect_event(
    frames: list[dict[str, Any]],
    summary: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any] | None:
    latest = frames[-1]
    previous = frames[-2] if len(frames) > 1 else latest
    opponent_profile = compact_opponent_profile(latest["opponents"])

    candidates: list[dict[str, Any]] = []

    if latest["lap"] > state["last_lap"]:
        candidates.append(
            {
                "event_type": "lap_complete",
                "reason": f"Completed lap {latest['lap'] - 1}",
                "priority": EVENT_PRIORITIES["lap_complete"],
                "completed_lap": latest["lap"] - 1,
            }
        )

    if latest["race_pos"] != state["last_race_pos"]:
        direction = "up" if latest["race_pos"] < state["last_race_pos"] else "down"
        candidates.append(
            {
                "event_type": "position_change",
                "reason": f"Position changed {direction} to P{latest['race_pos']}",
                "priority": EVENT_PRIORITIES["position_change"],
            }
        )

    damage_delta = latest["damage"] - state["last_damage"]
    if damage_delta >= 5.0:
        candidates.append(
            {
                "event_type": "contact",
                "reason": f"Damage jumped by {damage_delta:.1f}",
                "priority": EVENT_PRIORITIES["contact"],
            }
        )

    if abs(latest["track_pos"]) > 1.0 and not state["was_off_track"]:
        side = "left" if latest["track_pos"] < 0 else "right"
        candidates.append(
            {
                "event_type": "off_track",
                "reason": f"Car ran wide over the {side} edge",
                "priority": EVENT_PRIORITIES["off_track"],
            }
        )

    if opponent_profile["front_gap"] < 10.0 and latest["speed_x"] > 60.0:
        candidates.append(
            {
                "event_type": "battle",
                "reason": f"Front gap down to {opponent_profile['front_gap']:.1f} m",
                "priority": EVENT_PRIORITIES["battle"],
            }
        )

    if latest["speed_x"] - previous["speed_x"] > 22.0 and latest["throttle"] > 0.8:
        candidates.append(
            {
                "event_type": "pace_surge",
                "reason": f"Acceleration burst from {previous['speed_x']:.1f} to {latest['speed_x']:.1f} km/h",
                "priority": EVENT_PRIORITIES["pace_surge"],
            }
        )

    if latest["sim_time"] - state["last_commentary_sim_time"] >= BASELINE_INTERVAL:
        candidates.append(
            {
                "event_type": "pace_update",
                "reason": "General race rhythm update",
                "priority": EVENT_PRIORITIES["pace_update"],
            }
        )

    if not candidates:
        return None
    return max(candidates, key=lambda event: event["priority"])


def build_commentary_payload(
    frames: list[dict[str, Any]],
    summary: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    latest = frames[-1]
    return {
        "task": "race_commentary",
        "event_type": event["event_type"],
        "event_reason": event["reason"],
        "event_time": round(latest["sim_time"], 3),
        "current_state": compact_state_for_commentary(latest),
        "window_summary": {
            "avg_speed": round(summary.get("avg_speed", 0.0), 3),
            "speed_delta": round(summary.get("speed_delta", 0.0), 3),
            "damage_delta": round(summary.get("damage_delta", 0.0), 3),
            "nearest_opponent_now": round(summary.get("nearest_opponent_now", 200.0), 3),
        },
        "track_profile": compact_track_profile(latest["track"]),
        "opponent_profile": compact_opponent_profile(latest["opponents"]),
        "style": {
            "tone": "exciting but concise",
            "max_words": 28,
            "audience": "students watching a live F1 simulator demo",
        },
    }


def call_model(connection: Any, payload: dict[str, Any]) -> str:
    prompt = f"""You are a live motorsport commentator for a TORCS student project.
Read the structured event payload and produce one short line of commentary.

Rules:
- Maximum 28 words.
- Sound energetic and broadcast-ready.
- Mention the event clearly.
- Prefer one strong sentence over multiple clauses.
- No bullet points or quotation marks.

Event payload:
{json.dumps(payload, ensure_ascii=True)}"""

    return chat_completion_text(
        connection,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5,
        max_tokens=70,
        timeout=10.0,
    )


def should_emit_commentary(history: dict[str, float], text: str, current_sim_time: float) -> bool:
    key = normalize_text_key(text)
    if not key:
        return True
    previous = history.get(key)
    if previous is not None and current_sim_time - previous < COMMENTARY_DEDUPE_SECONDS:
        return False
    history[key] = current_sim_time
    return True


def handle_completed_commentary(
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
        print(f"[Commentary Worker] {result.error}")
        return
    commentary = str(result.output).strip()
    if not commentary or not should_emit_commentary(history, commentary, current_sim_time):
        return
    print(f"\n[COMMENTARY {result.task['event_type']} @ {requested_sim_time:.1f}s] {commentary}")
    speak_text(
        commentary,
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
    worker = LatestTaskRunner(lambda task: call_model(connection, task["payload"]), "race-commentator")

    state = {
        "last_lap": -1,
        "last_race_pos": 99,
        "last_damage": 0.0,
        "was_off_track": False,
        "last_commentary_sim_time": 0.0,
        "last_event_wall_clock": 0.0,
        "event_history": {},
    }
    commentary_history: dict[str, float] = {}

    print_connection_banner(connection, "TORCS Race Commentator")
    print(f"Event cooldown: {EVENT_COOLDOWN}s | Baseline interval: {BASELINE_INTERVAL}s | Loop interval: {LOOP_INTERVAL}s")

    speak_text(
        "Race commentator is ready.",
        enabled=ENABLE_TTS,
        voice=TTS_VOICE,
        rate=TTS_RATE,
        pulse_server=PULSE_SERVER,
    )

    while True:
        try:
            time.sleep(LOOP_INTERVAL)
            frames = collector.snapshot()
            if len(frames) < 12:
                continue

            while True:
                completed = worker.pop_completed()
                if completed is None:
                    break
                handle_completed_commentary(collector, commentary_history, completed)

            recent_frames = select_recent_frames(frames, WINDOW_SECONDS)
            if len(recent_frames) < 6:
                continue

            latest = recent_frames[-1]
            summary = summarize_frames(recent_frames)

            if state["last_lap"] == -1:
                state["last_lap"] = latest["lap"]
                state["last_race_pos"] = latest["race_pos"]
                state["last_damage"] = latest["damage"]
                state["was_off_track"] = abs(latest["track_pos"]) > 1.0
                state["last_commentary_sim_time"] = latest["sim_time"]
                continue

            now = time.time()
            event = detect_event(recent_frames, summary, state)
            if event is not None:
                signature = event_signature(event)
                previous_event_time = state["event_history"].get(signature, -10**9)
                event_cooldown = EVENT_COOLDOWNS.get(event["event_type"], EVENT_COOLDOWN)
                if now - state["last_event_wall_clock"] >= EVENT_COOLDOWN and latest["sim_time"] - previous_event_time >= event_cooldown:
                    worker.submit(
                        {
                            "event_type": event["event_type"],
                            "requested_sim_time": latest["sim_time"],
                            "payload": build_commentary_payload(recent_frames, summary, event),
                        },
                        priority=event["priority"],
                    )
                    state["event_history"][signature] = latest["sim_time"]
                    state["last_event_wall_clock"] = now
                    state["last_commentary_sim_time"] = latest["sim_time"]

            state["last_lap"] = max(state["last_lap"], latest["lap"])
            state["last_race_pos"] = latest["race_pos"]
            state["last_damage"] = latest["damage"]
            state["was_off_track"] = abs(latest["track_pos"]) > 1.0
        except KeyboardInterrupt:
            print("\nRace commentator stopped.")
            break
        except Exception as exc:
            print(f"[Commentator] Error: {exc}")


if __name__ == "__main__":
    main()
