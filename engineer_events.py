#!/usr/bin/env python3
"""
Direction 1 (B同学) addition: engineer incident memory.

chat_engineer's car_state is only ever "this instant" (see car_state_source.py)
-- it has no memory, so a driver asking "what just happened to me?" a few
seconds after going off track or taking damage gets nothing useful, because
by the time the question reaches the model the car_state may already look
normal again. This module tracks a short rolling history of incidents
(off-track excursions, damage jumps) heuristically from the same car_state
contract chat_engineer.py already depends on.

Deliberately independent of commentary_engine.py's own event detection
(recent_events/event_history) -- that stream only records anything while
the Commentary feature is enabled and its mode isn't "off" (see
runtime.py::_auto_commentary_loop), so relying on it would leave the
engineer's memory silently empty in an Engineer-only session. Tracking
incidents here from the same car_state chat_engineer.py already receives
keeps this feature working regardless of Commentary's on/off state.
"""

from __future__ import annotations

import time
from typing import Callable

OFF_TRACK_THRESHOLD = 1.0      # |track_pos| beyond this counts as off track (matches race_analyzer.py)
DAMAGE_JUMP_THRESHOLD = 200.0  # damage increase in one update big enough to count as its own incident
MAX_RECENT_EVENTS = 10


class IncidentTracker:
    """Stateful accumulator: short rolling history of off-track excursions
    and damage jumps, edge-triggered so a sustained off-track spell or a
    steady-state damage reading doesn't spam the same incident repeatedly.

    Call update(car_state) every time a fresh car_state becomes available
    (currently: each POST /api/engineer/ask -- see runtime.py), same
    cadence as tire_strategy.py's RaceStrategyTracker.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._was_off_track: bool = False
        self._last_damage: float | None = None
        self._events: list[dict] = []

    def update(self, car_state: dict) -> None:
        now = self._clock()
        track_pos = float(car_state.get("track_pos", 0.0))
        damage = float(car_state.get("damage", 0.0))
        is_off_track = abs(track_pos) > OFF_TRACK_THRESHOLD

        # First-ever update just seeds state -- whatever the car happens to
        # be doing when questions start isn't a "new" incident (same
        # seed-without-emitting pattern as commentary_engine.py's
        # CommentaryEngine._seed_state).
        if self._last_damage is not None:
            if is_off_track and not self._was_off_track:
                self._record("off_track", f"went off track (track position {track_pos:.2f})", now)

            damage_delta = damage - self._last_damage
            if damage_delta >= DAMAGE_JUMP_THRESHOLD:
                self._record("damage", f"took damage (+{damage_delta:.0f})", now)

        self._was_off_track = is_off_track
        self._last_damage = damage

    def _record(self, kind: str, detail: str, at: float) -> None:
        self._events.append({"type": kind, "detail": detail, "at": at})
        self._events = self._events[-MAX_RECENT_EVENTS:]

    @property
    def recent_events(self) -> list[dict]:
        """Recent incidents, newest first, with 'at' converted to
        seconds_ago so format_car_state stays a pure function of its input
        dict instead of needing its own clock."""
        now = self._clock()
        return [
            {"type": e["type"], "detail": e["detail"], "seconds_ago": round(now - e["at"], 1)}
            for e in reversed(self._events)
        ]
