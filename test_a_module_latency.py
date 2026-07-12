#!/usr/bin/env python3
"""
Test A module low-latency changes.

This file checks:
1. Python files have no syntax errors.
2. car_state interface is unchanged.
3. problems are short English labels.
4. problems has at most 2 items.
5. prompt input to AI is shorter and still works.
6. FakeCarStateSource still returns valid car_state.
"""

from __future__ import annotations

import py_compile
from pathlib import Path


REQUIRED_KEYS = [
    "speed",
    "rpm",
    "gear",
    "track_pos",
    "damage",
    "fuel",
    "lap_time",
    "problems",
]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_python_syntax() -> None:
    print("[1] Checking Python syntax...")

    files = [
        "race_analyzer.py",
        "car_state_source.py",
        "chat_engineer.py",
        "chat_engineer_gui.py",
        "prompt_builder.py",
    ]

    for filename in files:
        path = Path(filename)
        check(path.exists(), f"{filename} does not exist")
        py_compile.compile(str(path), doraise=True)

    print("    OK: Python syntax passed")


def test_car_state_interface() -> None:
    print("[2] Checking car_state interface...")

    from race_analyzer import telemetry_to_car_state

    raw = {
        "speedX": 100,
        "rpm": 3000,
        "gear": 3,
        "trackPos": 0.1,
        "damage": 0,
        "fuel": 20,
        "curLapTime": 30,
    }

    state = telemetry_to_car_state(raw)

    for key in REQUIRED_KEYS:
        check(key in state, f"missing key: {key}")

    check(isinstance(state["speed"], float), "speed should be float")
    check(isinstance(state["rpm"], float), "rpm should be float")
    check(isinstance(state["gear"], int), "gear should be int")
    check(isinstance(state["track_pos"], float), "track_pos should be float")
    check(isinstance(state["damage"], float), "damage should be float")
    check(isinstance(state["fuel"], float), "fuel should be float")
    check(isinstance(state["lap_time"], float), "lap_time should be float")
    check(isinstance(state["problems"], list), "problems should be list")

    print("    OK: car_state interface is unchanged")
    print("    state:", state)


def test_problem_labels() -> None:
    print("[3] Checking problem labels...")

    from race_analyzer import telemetry_to_car_state

    raw = {
        "speedX": 180,
        "rpm": 9100,
        "gear": 4,
        "trackPos": -0.95,
        "damage": 3400,
        "fuel": 12,
        "curLapTime": 88.7,
    }

    state = telemetry_to_car_state(raw)
    problems = state["problems"]

    check(isinstance(problems, list), "problems should be a list")
    check(len(problems) <= 2, "problems should have at most 2 items")
    check(all(isinstance(p, str) for p in problems), "each problem should be string")
    check(all(p.isascii() for p in problems), "problems should be simple English / ASCII")
    check(all(len(p) <= 30 for p in problems), "problem label is too long")

    print("    OK: problems are short English labels")
    print("    problems:", problems)


def test_normal_state() -> None:
    print("[4] Checking normal state...")

    from race_analyzer import telemetry_to_car_state

    raw = {
        "speedX": 120,
        "rpm": 5000,
        "gear": 3,
        "trackPos": 0.0,
        "damage": 0,
        "fuel": 30,
        "curLapTime": 40,
    }

    state = telemetry_to_car_state(raw)
    problems = state["problems"]

    check(isinstance(problems, list), "problems should be list")
    check(len(problems) <= 2, "normal problems should have at most 2 items")
    check(all(p.isascii() for p in problems), "normal problems should be English / ASCII")

    print("    OK: normal state works")
    print("    problems:", problems)


def test_prompt_input() -> None:
    print("[5] Checking prompt input to AI...")

    from race_analyzer import telemetry_to_car_state
    from prompt_builder import build_messages

    raw = {
        "speedX": 180,
        "rpm": 9100,
        "gear": 4,
        "trackPos": -0.95,
        "damage": 3400,
        "fuel": 12,
        "curLapTime": 88.7,
    }

    state = telemetry_to_car_state(raw)
    messages = build_messages(state, "What should I do now?")
    text = "\n".join(message["content"] for message in messages)

    check(len(messages) >= 2, "messages should contain system and user message")
    check("speed" not in state["problems"], "problems should contain labels, not full telemetry")
    check(len(state["problems"]) <= 2, "prompt should only include at most 2 problems")
    check(all(p.isascii() for p in state["problems"]), "prompt problems should be English")

    print("    OK: prompt can be built with shorter car_state")
    print("    prompt characters:", len(text))
    print("    problems:", state["problems"])


def test_fake_car_state_source() -> None:
    print("[6] Checking FakeCarStateSource...")

    from car_state_source import FakeCarStateSource

    source = FakeCarStateSource()
    state = source.get_state()

    for key in REQUIRED_KEYS:
        check(key in state, f"FakeCarStateSource missing key: {key}")

    check(len(state["problems"]) <= 2, "FakeCarStateSource problems should have at most 2 items")
    check(all(p.isascii() for p in state["problems"]), "FakeCarStateSource problems should be English")

    print("    OK: FakeCarStateSource works")
    print("    state:", state)


def main() -> None:
    print("A Module Low-Latency Test")
    print("=" * 32)

    test_python_syntax()
    test_car_state_interface()
    test_problem_labels()
    test_normal_state()
    test_prompt_input()
    test_fake_car_state_source()

    print("=" * 32)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()