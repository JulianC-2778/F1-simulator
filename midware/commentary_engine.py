"""
Race-event detection and prompt payload construction for commentary.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from event_payload_config import EVENT_FIELDS


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


@dataclass
class CommentaryConfig:
    mode: str = "interval"  # off | interval | event | hybrid
    baseline_interval: float = 10.0
    event_cooldown: float = 1.0
    window_seconds: float = 6.0
    dedupe_seconds: float = 10.0
    max_words: int = 45


@dataclass
class CommentaryDecision:
    event: dict[str, Any]
    payload: dict[str, Any]


@dataclass
class CommentaryEngine:
    config: CommentaryConfig = field(default_factory=CommentaryConfig)
    last_lap: int = -1
    last_race_pos: int = 99
    last_damage: float = 0.0
    was_off_track: bool = False
    last_commentary_sim_time: float = 0.0
    last_event_wall_clock: float = 0.0
    event_history: dict[str, float] = field(default_factory=dict)
    text_history: dict[str, float] = field(default_factory=dict)
    recent_events: list[dict[str, Any]] = field(default_factory=list)

    def update_config(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            if hasattr(self.config, key):
                current = getattr(self.config, key)
                if isinstance(current, float):
                    value = float(value)
                elif isinstance(current, int):
                    value = int(value)
                else:
                    value = str(value)
                setattr(self.config, key, value)

    def next_decision(
        self,
        frames: list[dict[str, Any]],
        rankings: list[dict[str, Any]] | None = None,
    ) -> CommentaryDecision | None:
        if self.config.mode == "off" or not frames:
            return None

        latest = normalize_frame(frames[-1])
        if self.last_lap == -1:
            self._seed_state(latest)
            return None
        if len(frames) < 2:
            return None

        window_frames = select_recent_frames(frames, self.config.window_seconds)
        if len(window_frames) < 2:
            return None
        normalized_window = [normalize_frame(frame) for frame in window_frames]
        summary = summarize_frames(normalized_window)

        event = detect_event(normalized_window, summary, self)
        if event is None:
            self._update_state(latest)
            return None

        if self.config.mode == "interval" and event["event_type"] != "pace_update":
            self._update_state(latest)
            return None
        if self.config.mode == "event" and event["event_type"] == "pace_update":
            self._update_state(latest)
            return None

        if not self._can_emit_event(event, latest["sim_time"]):
            self._update_state(latest)
            return None

        self.last_commentary_sim_time = latest["sim_time"]
        self.last_event_wall_clock = time.time()
        self.event_history[event_signature(event)] = latest["sim_time"]
        self._remember_event(event, latest["sim_time"])
        payload = build_commentary_payload(normalized_window, summary, event, rankings, self.config.max_words)
        self._update_state(latest)
        return CommentaryDecision(event=event, payload=payload)

    def should_emit_text(self, text: str, sim_time: float) -> bool:
        key = normalize_text_key(text)
        if not key:
            return True
        previous = self.text_history.get(key)
        if previous is not None and sim_time - previous < self.config.dedupe_seconds:
            return False
        self.text_history[key] = sim_time
        return True

    def _seed_state(self, latest: dict[str, Any]) -> None:
        self.last_lap = latest["lap"]
        self.last_race_pos = latest["race_pos"]
        self.last_damage = latest["damage"]
        self.was_off_track = abs(latest["track_pos"]) > 1.0
        self.last_commentary_sim_time = latest["sim_time"]

    def _update_state(self, latest: dict[str, Any]) -> None:
        self.last_lap = max(self.last_lap, latest["lap"])
        self.last_race_pos = latest["race_pos"]
        self.last_damage = latest["damage"]
        self.was_off_track = abs(latest["track_pos"]) > 1.0

    def _can_emit_event(self, event: dict[str, Any], sim_time: float) -> bool:
        now = time.time()
        if now - self.last_event_wall_clock < self.config.event_cooldown:
            return False
        signature = event_signature(event)
        previous_event_time = self.event_history.get(signature, -10**9)
        event_cooldown = EVENT_COOLDOWNS.get(event["event_type"], self.config.event_cooldown)
        return sim_time - previous_event_time >= event_cooldown

    def _remember_event(self, event: dict[str, Any], sim_time: float) -> None:
        stored = {**event, "sim_time": round(sim_time, 3)}
        self.recent_events.append(stored)
        self.recent_events = self.recent_events[-25:]


def normalize_text_key(text: str, max_words: int = 12) -> str:
    normalized = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    collapsed = " ".join(normalized.split())
    return " ".join(collapsed.split()[:max_words])


def event_signature(event: dict[str, Any]) -> str:
    return normalize_text_key(f"{event['event_type']} {event['reason']}", max_words=16)


def number(frame: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in frame and frame[key] is not None:
            try:
                return float(frame[key])
            except (TypeError, ValueError):
                return default
    return default


def normalize_frame(frame: dict[str, Any]) -> dict[str, Any]:
    opponents = [number(frame, f"opponent_{i}", default=200.0) for i in range(36)]
    track = [number(frame, f"track_{i}", default=-1.0) for i in range(19)]
    return {
        "sim_time": number(frame, "sim_time"),
        "lap": int(number(frame, "lap")),
        "speed_x": number(frame, "speed_x", "speedX"),
        "gear": int(number(frame, "gear")),
        "track_pos": number(frame, "track_pos", "trackPos"),
        "damage": number(frame, "damage"),
        "race_pos": int(number(frame, "race_pos", "racePos", default=99)),
        "fuel": number(frame, "fuel"),
        "throttle": number(frame, "throttle"),
        "brake": number(frame, "brake"),
        "steer": number(frame, "steer"),
        "angle": number(frame, "angle"),
        "rpm": number(frame, "rpm"),
        "dist_from_start": number(frame, "dist_from_start", "distFromStart"),
        "cur_lap_time": number(frame, "cur_lap_time", "curLapTime"),
        "last_lap_time": number(frame, "last_lap_time", "lastLapTime"),
        "opponents": opponents,
        "track": track,
    }


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def safe_min(values: list[float], default: float = 0.0) -> float:
    return min(values) if values else default


def select_recent_frames(frames: list[dict[str, Any]], window_seconds: float) -> list[dict[str, Any]]:
    if not frames:
        return []
    latest_time = number(frames[-1], "sim_time")
    cutoff = latest_time - window_seconds
    return [frame for frame in frames if number(frame, "sim_time") >= cutoff]


def compact_track_profile(track: list[float]) -> dict[str, float]:
    if len(track) < 19:
        return {"left_opening": -1.0, "center_opening": -1.0, "right_opening": -1.0, "tightest_opening": -1.0}
    return {
        "left_opening": round(mean(track[0:6]), 3),
        "center_opening": round(mean(track[7:12]), 3),
        "right_opening": round(mean(track[13:19]), 3),
        "tightest_opening": round(safe_min(track, -1.0), 3),
    }


def compact_opponent_profile(opponents: list[float]) -> dict[str, float]:
    if len(opponents) < 36:
        return {"front_gap": 200.0, "left_gap": 200.0, "right_gap": 200.0, "rear_gap": 200.0, "nearest_gap": 200.0}
    nearest = [distance for distance in opponents if distance >= 0]
    return {
        "front_gap": round(safe_min(opponents[16:21], 200.0), 3),
        "left_gap": round(safe_min(opponents[21:28], 200.0), 3),
        "right_gap": round(safe_min(opponents[9:16], 200.0), 3),
        "rear_gap": round(safe_min(opponents[0:4] + opponents[32:36], 200.0), 3),
        "nearest_gap": round(safe_min(nearest, 200.0), 3),
    }


def summarize_frames(frames: list[dict[str, Any]]) -> dict[str, Any]:
    if not frames:
        return {}
    latest = frames[-1]
    first = frames[0]
    speeds = [frame["speed_x"] for frame in frames]
    nearest_opponents = [
        min((distance for distance in frame["opponents"] if distance >= 0), default=200.0)
        for frame in frames
    ]
    return {
        "frame_count": len(frames),
        "duration": max(0.0, latest["sim_time"] - first["sim_time"]),
        "avg_speed": mean(speeds),
        "speed_delta": latest["speed_x"] - first["speed_x"],
        "damage_delta": latest["damage"] - first["damage"],
        "nearest_opponent_now": nearest_opponents[-1] if nearest_opponents else 200.0,
        "nearest_opponent_window": min(nearest_opponents) if nearest_opponents else 200.0,
    }


def detect_event(
    frames: list[dict[str, Any]],
    summary: dict[str, Any],
    state: CommentaryEngine,
) -> dict[str, Any] | None:
    latest = frames[-1]
    previous = frames[-2] if len(frames) > 1 else latest
    opponent_profile = compact_opponent_profile(latest["opponents"])
    candidates: list[dict[str, Any]] = []

    if latest["lap"] > state.last_lap:
        candidates.append({
            "event_type": "lap_complete",
            "reason": f"Completed lap {latest['lap'] - 1}",
            "priority": EVENT_PRIORITIES["lap_complete"],
            "completed_lap": latest["lap"] - 1,
        })

    if latest["race_pos"] != state.last_race_pos:
        direction = "up" if latest["race_pos"] < state.last_race_pos else "down"
        candidates.append({
            "event_type": "position_change",
            "reason": f"Position changed {direction} to P{latest['race_pos']}",
            "priority": EVENT_PRIORITIES["position_change"],
        })

    damage_delta = latest["damage"] - state.last_damage
    if damage_delta >= 5.0:
        candidates.append({
            "event_type": "contact",
            "reason": f"Damage jumped by {damage_delta:.1f}",
            "priority": EVENT_PRIORITIES["contact"],
        })

    if abs(latest["track_pos"]) > 1.0 and not state.was_off_track:
        side = "left" if latest["track_pos"] < 0 else "right"
        candidates.append({
            "event_type": "off_track",
            "reason": f"Car ran wide over the {side} edge",
            "priority": EVENT_PRIORITIES["off_track"],
        })

    if opponent_profile["front_gap"] < 10.0 and latest["speed_x"] > 60.0:
        candidates.append({
            "event_type": "battle",
            "reason": f"Front gap down to {opponent_profile['front_gap']:.1f} m",
            "priority": EVENT_PRIORITIES["battle"],
        })

    if latest["speed_x"] - previous["speed_x"] > 22.0 and latest["throttle"] > 0.8:
        candidates.append({
            "event_type": "pace_surge",
            "reason": f"Acceleration burst from {previous['speed_x']:.1f} to {latest['speed_x']:.1f} km/h",
            "priority": EVENT_PRIORITIES["pace_surge"],
        })

    if latest["sim_time"] - state.last_commentary_sim_time >= state.config.baseline_interval:
        candidates.append({
            "event_type": "pace_update",
            "reason": "General race rhythm update",
            "priority": EVENT_PRIORITIES["pace_update"],
        })

    if not candidates:
        return None
    return max(candidates, key=lambda event: event["priority"])


def _collision_direction(opp: dict[str, float]) -> str:
    gaps = {"front": opp["front_gap"], "left": opp["left_gap"],
            "right": opp["right_gap"], "rear": opp["rear_gap"]}
    return min(gaps, key=gaps.get)


def _infer_collision_partner(
    latest: dict[str, Any],
    direction: str,
    rankings: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not rankings:
        return None
    player_dist = float(latest.get("dist_from_start", 0.0))
    player_pos = int(latest.get("race_pos", 99))
    others = [r for r in rankings if int(r.get("race_pos", 99)) != player_pos]
    if not others:
        return None
    if direction == "front":
        candidates = [r for r in others if r.get("dist_from_start", 0) >= player_dist]
        if candidates:
            c = min(candidates, key=lambda r: r.get("dist_from_start", 0) - player_dist)
            return {"car_name": c.get("car_name", "?"), "race_pos": c.get("race_pos", "?")}
    elif direction == "rear":
        candidates = [r for r in others if r.get("dist_from_start", 0) <= player_dist]
        if candidates:
            c = min(candidates, key=lambda r: player_dist - r.get("dist_from_start", 0))
            return {"car_name": c.get("car_name", "?"), "race_pos": c.get("race_pos", "?")}
    c = min(others, key=lambda r: abs(r.get("dist_from_start", 0) - player_dist))
    return {"car_name": c.get("car_name", "?"), "race_pos": c.get("race_pos", "?")}


def _extract_fields(
    field_names: list[str],
    latest: dict[str, Any],
    summary: dict[str, Any],
    event: dict[str, Any],
    rankings: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    opp = compact_opponent_profile(latest["opponents"])
    col_dir = _collision_direction(opp)

    registry: dict[str, Any] = {
        "race_pos":            int(latest["race_pos"]),
        "lap":                 int(latest["lap"]),
        "gear":                int(latest["gear"]),
        "track_pos":           round(latest["track_pos"], 3),
        "fuel_remaining":      round(latest["fuel"], 3),
        "total_damage":        round(latest["damage"], 3),
        "damage_delta":        round(summary.get("damage_delta", 0.0), 3),
        "last_lap_time":       round(latest.get("last_lap_time", 0.0), 3),
        "front_gap":           round(opp["front_gap"], 3),
        "rear_gap":            round(opp["rear_gap"], 3),
        "nearest_gap":         round(opp["nearest_gap"], 3),
        "direction":           "up" if "up" in event.get("reason", "") else "down",
        "new_pos":             int(latest["race_pos"]),
        "side":                "right" if "right" in event.get("reason", "") else "left",
        "completed_lap":       event.get("completed_lap", int(latest["lap"]) - 1),
        "collision_direction": col_dir,
        "collision_partner":   _infer_collision_partner(latest, col_dir, rankings),
        "rankings":            [{"car_name": r.get("car_name", "?"),
                                 "race_pos": r.get("race_pos", "?")}
                                for r in (rankings or [])],
    }
    return {k: registry[k] for k in field_names if k in registry}


def build_commentary_payload(
    frames: list[dict[str, Any]],
    summary: dict[str, Any],
    event: dict[str, Any],
    rankings: list[dict[str, Any]] | None,
    max_words: int,
) -> dict[str, Any]:
    latest = frames[-1]
    event_type = event["event_type"]
    field_names = EVENT_FIELDS.get(event_type, ["race_pos", "lap"])

    payload: dict[str, Any] = {
        "task": "race_commentary",
        "event_type": event_type,
        "event_reason": event["reason"],
        "event_time": round(latest["sim_time"], 3),
    }
    payload.update(_extract_fields(field_names, latest, summary, event, rankings))
    payload["style"] = {
        "language": "zh-CN",
        "tone": "professional, vivid, concise",
        "max_words": max_words,
    }
    return payload
