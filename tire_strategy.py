#!/usr/bin/env python3
"""
Direction 1 (B同学) addition: tire wear + pit window estimation.

TORCS's SCR telemetry does not expose tire wear at all -- there's no field
for it anywhere in the car_state contract (car_state_source.py) or the raw
telemetry underneath it. The project brief's own example question is "how's
my tire wear?", so without this, the engineer can only refuse to answer or
make a number up (the persona explicitly forbids the latter). This module
estimates wear heuristically from the car_state contract alone, so it has
no dependency on race_analyzer.py or how "A同学" reads/analyzes raw TORCS
data -- only on the same dict shape chat_engineer.py already depends on.

This is a deliberate approximation, not a physics simulation -- TORCS
itself doesn't model degradation, so any estimate here is inherently a
simplification of real tire behaviour. The wear/pit numbers are meant to
give the AI real, self-consistent data to reason from instead of inventing
one, not to be taken as an accurate physical model.
"""

from __future__ import annotations

import time
from typing import Callable

WEAR_RATE_PER_SECOND = 0.02          # tuned so ~2000s (~25 laps at ~80s/lap) of normal driving reaches ~100%
CORNERING_MULTIPLIER = 8.0           # extra wear weight for lateral track-position movement (cornering proxy)
FUEL_INCREASE_RESET_THRESHOLD = 2.0  # litres -- a jump this big only happens on a refuel/pit stop
LAP_RESET_DROP_THRESHOLD = 5.0       # seconds -- lap_time dropping by more than this signals a new lap started

TIRE_WARNING_PCT = 65.0
TIRE_CRITICAL_PCT = 85.0

LOW_FUEL_LAPS_WARNING = 3.0
LOW_FUEL_LAPS_CRITICAL = 1.0


class RaceStrategyTracker:
    """Stateful accumulator: tire wear estimate + per-lap fuel consumption.

    Call update(car_state) every time a fresh car_state becomes available
    (currently: each POST /api/engineer/ask -- see runtime.py). Wear and
    fuel-consumption estimates therefore only advance as often as the
    engineer is actually asked something, not continuously in the
    background. Moving this to a continuous poll (like
    runtime.py::_auto_commentary_loop) is possible future work; not done
    here to keep this addition self-contained and avoid touching the
    shared polling infrastructure.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._wear_pct = 0.0
        self._last_update_at: float | None = None
        self._last_track_pos: float | None = None
        self._last_fuel: float | None = None
        self._last_lap_time: float | None = None
        self._lap_start_fuel: float | None = None
        self._last_lap_fuel_consumption: float | None = None

    def reset(self) -> None:
        """New tires -- e.g. called when update() detects a pit stop."""
        self._wear_pct = 0.0

    def update(self, car_state: dict) -> None:
        now = self._clock()
        speed = float(car_state.get("speed", 0.0))
        track_pos = float(car_state.get("track_pos", 0.0))
        fuel = float(car_state.get("fuel", 0.0))
        lap_time = float(car_state.get("lap_time", 0.0))

        dt = 0.0 if self._last_update_at is None else max(0.0, now - self._last_update_at)
        self._last_update_at = now

        # Pit-stop heuristic: car_state has no explicit "just pitted" signal,
        # but fuel only decreases during normal driving -- a jump this size
        # only happens on a refuel, which comes with fresh tires. Zero dt too
        # so the interval that just ended in a pit stop doesn't also pile old-
        # tire wear onto the fresh tires in this same update.
        if self._last_fuel is not None and fuel > self._last_fuel + FUEL_INCREASE_RESET_THRESHOLD:
            self.reset()
            dt = 0.0

        if dt > 0:
            cornering = 0.0 if self._last_track_pos is None else abs(track_pos - self._last_track_pos)
            speed_factor = max(0.0, speed) / 100.0
            increment = dt * WEAR_RATE_PER_SECOND * (1.0 + speed_factor) * (1.0 + cornering * CORNERING_MULTIPLIER)
            self._wear_pct = min(100.0, self._wear_pct + increment)

        # New-lap heuristic for fuel-per-lap: car_state has no lap counter,
        # but lap_time resetting to near-zero after being substantial is a
        # reliable enough signal that a new lap just started.
        if self._last_lap_time is not None and lap_time < self._last_lap_time - LAP_RESET_DROP_THRESHOLD:
            if self._lap_start_fuel is not None:
                self._last_lap_fuel_consumption = max(0.0, self._lap_start_fuel - fuel)
            self._lap_start_fuel = fuel
        elif self._lap_start_fuel is None:
            self._lap_start_fuel = fuel

        self._last_track_pos = track_pos
        self._last_fuel = fuel
        self._last_lap_time = lap_time

    @property
    def wear_pct(self) -> float:
        return round(self._wear_pct, 1)

    @property
    def fuel_per_lap(self) -> float | None:
        return None if self._last_lap_fuel_consumption is None else round(self._last_lap_fuel_consumption, 2)


def estimate_pit_window(car_state: dict, wear_pct: float, fuel_per_lap: float | None) -> dict:
    """Pure function: given the current data + tracker readings, decide
    whether to recommend a pit stop and why.

    Deliberately does all the arithmetic in Python rather than asking the
    model to compare numbers itself -- the same "compute the real answer,
    let the model only phrase it" pattern race_analyzer.analyze_car_state
    already uses for problem detection, and the direct fix for the failure
    mode a real model showed during manual testing (getting fuel/damage
    threshold comparisons wrong on its own).
    """
    fuel = float(car_state.get("fuel", 0.0))
    reasons: list[str] = []
    urgency = "low"

    if wear_pct >= TIRE_CRITICAL_PCT:
        reasons.append("tires")
        urgency = "high"
    elif wear_pct >= TIRE_WARNING_PCT:
        reasons.append("tires")
        urgency = "medium"

    laps_of_fuel_left = None
    if fuel_per_lap and fuel_per_lap > 0:
        laps_of_fuel_left = round(fuel / fuel_per_lap, 1)
        if laps_of_fuel_left <= LOW_FUEL_LAPS_CRITICAL:
            reasons.append("fuel")
            urgency = "high"
        elif laps_of_fuel_left <= LOW_FUEL_LAPS_WARNING:
            reasons.append("fuel")
            if urgency != "high":
                urgency = "medium"

    return {
        "recommend_pit": urgency in ("medium", "high"),
        "urgency": urgency,
        "reasons": reasons,
        "tire_wear_pct": round(wear_pct, 1),
        "laps_of_fuel_left": laps_of_fuel_left,
    }
