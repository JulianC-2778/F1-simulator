#!/usr/bin/env python3
"""
Independent re-implementation of the car_state pipeline's expected behaviour.

Deliberately NOT copied from race_analyzer.py -- written separately from
docs/event-payload-reference.md and docs/commentary_test_matrix.md (the
team's own documented spec for these fields) so that comparing this
module's output against race_analyzer.py's *actual* output is a genuine
check on race_analyzer.py's implementation, not race_analyzer.py compared
against itself. If race_analyzer.py has a bug (wrong field name, wrong
comparison operator, wrong priority order, a dropped condition), this
module's independently-derived answer will disagree with it and the
checker scripts in this folder will flag the mismatch.

This only covers what the checker scripts need: raw-field renaming and the
problem-detection thresholds. It is not a drop-in replacement for
race_analyzer.py.
"""

from __future__ import annotations

from typing import Any

# Raw TORCS field name -> car_state field name. Values are copied as-is
# (no unit conversion) -- see docs/commentary-loop.md and
# docs/event-payload-reference.md.
_FIELD_MAP = {
    "speedX": "speed",
    "rpm": "rpm",
    "gear": "gear",
    "trackPos": "track_pos",
    "damage": "damage",
    "fuel": "fuel",
    "curLapTime": "lap_time",
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def expected_car_state(raw: dict) -> dict:
    """Rename raw TORCS fields into the car_state shape. Missing/malformed
    fields fall back to 0 (docs don't specify a different default), never
    raise."""
    return {
        "speed": _as_float(raw.get("speedX")),
        "rpm": _as_float(raw.get("rpm")),
        "gear": _as_int(raw.get("gear")),
        "track_pos": _as_float(raw.get("trackPos")),
        "damage": _as_float(raw.get("damage")),
        "fuel": _as_float(raw.get("fuel")),
        "lap_time": _as_float(raw.get("curLapTime")),
    }


# (severity, label, condition) -- higher severity wins when several trigger.
# Numbers and order match docs/event-payload-reference.md +
# docs/commentary_test_matrix.md, re-derived by hand rather than read out of
# race_analyzer.py's source.
def expected_problems(car_state: dict) -> list[str]:
    speed = car_state.get("speed", 0.0)
    rpm = car_state.get("rpm", 0.0)
    gear = car_state.get("gear", 0)
    track_pos = car_state.get("track_pos", 0.0)
    damage = car_state.get("damage", 0.0)
    fuel = car_state.get("fuel", 0.0)

    hits: list[tuple[int, str]] = []

    if abs(track_pos) > 1.0:
        hits.append((100, "off track"))
    elif abs(track_pos) > 0.8:
        hits.append((80, "near track edge"))

    if damage > 3000:
        hits.append((90, "car damage high"))
    elif damage > 1500:
        hits.append((60, "car damage medium"))

    # Note: fuel == 0 exactly does NOT trigger "fuel low" -- the documented
    # rule is a strict open interval (0, 8), not [0, 8). Zero fuel is a
    # separate, undocumented state; flagged as a known gap, not assumed to
    # be a bug.
    if 0 < fuel < 8:
        hits.append((85, "fuel low"))

    if rpm > 8500:
        hits.append((70, "rpm too high"))
    elif rpm < 2500 and gear > 2:
        hits.append((50, "rpm too low"))

    if speed < 80 and gear > 3:
        hits.append((45, "gear too high"))

    if not hits:
        return ["normal"]

    hits.sort(key=lambda item: item[0], reverse=True)
    return [label for _, label in hits[:2]]


def expected_pipeline_output(raw: dict) -> dict:
    """Full expected result for a raw telemetry frame: car_state + problems,
    in the same shape race_analyzer.telemetry_to_car_state() returns."""
    state = expected_car_state(raw)
    state["problems"] = expected_problems(state)
    return state
