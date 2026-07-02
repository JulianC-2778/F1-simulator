#!/usr/bin/env python3
"""
Feature 1 (AI Racing Engineer Chatbot): prompt construction.

Turns a car_state dict (see car_state_source.py for the contract) plus a
player question into the chat-completion `messages` list sent to Granite.
"""

from __future__ import annotations

from typing import Any


SYSTEM_PROMPT = (
    "You are a professional, direct-talking AI racing engineer on the radio with a TORCS driver. "
    "Answer only using the live telemetry and detected issues provided below -- never invent numbers "
    "that were not given. Sound like a real pit-wall radio call: interpret the data and tell the driver "
    "what to do about it, don't just read the numbers back. Match your length to what was asked -- a single "
    "question gets 2-3 sentences with a bit of reasoning, not just a bare fact. If the driver asks about "
    "several things at once, keep each part shorter so the whole answer doesn't run long, and answer them "
    "in order of importance to the race, not necessarily the order they were asked. If a question has "
    "nothing to do with the car, the race, or the data provided, deprioritize it -- answer it last and "
    "briefly, or skip it if it doesn't matter. Never pad, ramble, or repeat yourself. Always answer in English."
)


def format_car_state(car_state: dict[str, Any]) -> str:
    problems = car_state.get("problems") or []
    problem_text = ", ".join(problems) if problems else "No issues detected."
    return (
        f"Speed: {car_state.get('speed', 0):.0f} km/h\n"
        f"RPM: {car_state.get('rpm', 0):.0f}\n"
        f"Gear: {car_state.get('gear', 0)}\n"
        f"Track position: {car_state.get('track_pos', 0):.2f} (0 = center line, closer to +/-1 = closer to the track edge)\n"
        f"Damage: {car_state.get('damage', 0):.0f}\n"
        f"Fuel remaining: {car_state.get('fuel', 0):.1f} L\n"
        f"Current lap time: {car_state.get('lap_time', 0):.1f} s\n"
        f"Detected issues: {problem_text}"
    )


def build_messages(
    car_state: dict[str, Any],
    user_question: str,
    history: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Build the chat-completion messages list for one turn.

    `history` is an optional list of prior {"role": ..., "content": ...}
    turns from this same session. The caller (chat_engineer.py) is
    responsible for trimming it so older laps don't dominate the context
    window.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append(
        {
            "role": "user",
            "content": f"Current car data:\n{format_car_state(car_state)}\n\nDriver's question:\n{user_question}",
        }
    )
    return messages
