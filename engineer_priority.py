#!/usr/bin/env python3
"""
Direction 1 (B同学) addition: unified priority synthesis.

tire_strategy.py's estimate_pit_window() and engineer_events.py's
IncidentTracker each compute their own slice of "what's going on" -- tire/
fuel urgency, and a rolling incident history -- but ask_engineer() just
hands the model all of it side by side and trusts an 8B local model to
weigh which one matters most right now. That's the same failure mode
tire_strategy.py's own module docstring already names (a real model getting
threshold comparisons wrong on its own), one level up: comparing across
*categories* of concern (pit urgency vs. a fresh incident vs. currently
being off track) is a harder judgment call than comparing two numbers, so
leaving it to the model is even riskier, not less. This module computes the
single top-priority conclusion in Python from data tire_strategy.py,
engineer_events.py, and race_analyzer.py's "problems" list already
produced -- the model only phrases it.
"""

from __future__ import annotations

# race_analyzer.analyze_car_state()'s labels for "car is currently off track
# or right at the edge" -- a driving correction, not a pit-stop matter, per
# ENGINEER_PERSONA_TAIL's existing rule in context_manager.py. This beats
# every other signal below because it's an immediate physical situation,
# not a strategic one.
OFF_TRACK_PROBLEM_LABELS = {"off track", "near track edge"}

# An incident newer than this is still worth a mention even if nothing else
# is urgent; older than this and it's stale context, not "right now".
FRESH_INCIDENT_WINDOW_SECONDS = 15.0


def summarize_priority(car_state: dict, pit_window: dict, recent_incidents: list[dict]) -> dict:
    """Pure function: given already-computed signals, decide the single
    thing the driver should focus on right now, and why.

    Same "compute the real answer in Python, let the model only phrase it"
    pattern estimate_pit_window() already uses for pit-window math.
    """
    problems = car_state.get("problems") or []

    if any(label in OFF_TRACK_PROBLEM_LABELS for label in problems):
        return {
            "top_priority": "get back on track",
            "severity": "high",
            "reason": "car is currently off track / near the edge",
            # "physical" = an immediate driving-correction danger, distinct
            # from "strategic" (a pit-stop decision) -- alarm-fatigue research
            # (aviation/medical HMI) says alerts should be told apart by kind,
            # not just severity, so a proactive alert can treat these
            # differently instead of announcing both the same way (see
            # _next_engineer_alert in runtime.py).
            "category": "physical",
        }

    urgency = pit_window.get("urgency")
    if urgency == "high":
        return {
            "top_priority": "pit now",
            "severity": "high",
            "reason": ", ".join(pit_window.get("reasons") or []) or "pit window",
            "category": "strategic",
        }
    if urgency == "medium":
        return {
            "top_priority": "plan a pit stop soon",
            "severity": "medium",
            "reason": ", ".join(pit_window.get("reasons") or []) or "pit window",
            "category": "strategic",
        }

    fresh = [
        incident for incident in recent_incidents
        if incident.get("seconds_ago", float("inf")) <= FRESH_INCIDENT_WINDOW_SECONDS
    ]
    if fresh:
        return {
            "top_priority": "no urgent action, but note a recent incident",
            "severity": "low",
            "reason": fresh[0].get("detail", ""),
            "category": "informational",
        }

    return {
        "top_priority": "no urgent priority -- car is in good shape",
        "severity": "low",
        "reason": "",
        "category": "informational",
    }
