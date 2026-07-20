#!/usr/bin/env python3
"""
Feature 1: AI Racing Engineer Chatbot (CLI).

TORCS telemetry -> car_state contract -> midware.context_manager -> Granite -> answer

This is "B 同学" 's deliverable for Module 1 per the team's 分工文档: AI
赛车工程师问答功能. It consumes the car_state contract defined in
car_state_source.py and does not depend on how "A 同学" eventually reads
and analyzes TORCS data -- only on that dict shape.

Prompt construction and conversation-history trimming are shared with the
main commentary pipeline via midware.context_manager.ContextManager (token-
budget based trimming) instead of a separate prompt_builder.py.

Run:
    python3 chat_engineer.py

On startup, this first probes midware's REST API (GET /api/telemetry) for live
telemetry -- if midware/commentary.py is already running, this avoids binding
UDP directly and colliding with it on port 3101 (see
docs/feature2-standalone-service.md for the same pattern applied to Module 2's
dashboard service). Only falls back to binding UDP itself if midware isn't
reachable, so this script still works standalone.

Env vars:
    Model configuration is owned by the Middleware Model Broker.
    TORCS_ENGINEER_USE_FAKE_DATA     - "true" to force demo data instead of live telemetry
    TORCS_ENGINEER_MIDWARE_URL       - midware base URL to probe first (default from config.py)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import overlay_broadcast
import voice_input
from car_state_source import (
    CarStateSource,
    FakeCarStateSource,
    HttpCarStateSource,
    wait_for_live_state,
)
from midware.client import ask_engineer
from midware.context_manager import format_car_state
from telemetry_common import env_flag


USE_FAKE_DATA = env_flag("TORCS_ENGINEER_USE_FAKE_DATA", False)
MIDWARE_BASE_URL = os.getenv("TORCS_ENGINEER_MIDWARE_URL", config.MIDWARE_BASE_URL)


def choose_car_state_source() -> CarStateSource:
    if USE_FAKE_DATA:
        print("[ChatEngineer] TORCS_ENGINEER_USE_FAKE_DATA=true -> using demo car_state data.")
        return FakeCarStateSource()

    # Production telemetry is owned by middleware; this legacy CLI is API-only.
    midware_source = HttpCarStateSource(base_url=MIDWARE_BASE_URL)
    print(f"[ChatEngineer] Probing midware at {MIDWARE_BASE_URL} for live telemetry (no UDP bind)...")
    if wait_for_live_state(midware_source, timeout=2.0):
        print("[ChatEngineer] Live telemetry detected via midware, using real car_state data.")
        return midware_source

    print(
        "[ChatEngineer] Middleware has no live telemetry. Falling back to demo data; "
        "start the main backend with `python3 midware/commentary.py`, "
        "or set TORCS_ENGINEER_USE_FAKE_DATA=true to silence this message."
    )
    return FakeCarStateSource()


def print_car_state(car_state: dict[str, Any]) -> None:
    print("\n" + format_car_state(car_state))


def main() -> None:
    print("DEPRECATED DEBUG CLIENT: production API is POST /api/engineer/ask; removal target: v2.")
    car_state_source = choose_car_state_source()

    print(
        "\n输入你的问题（例如：我的轮胎状态怎么样？/ 现在该不该进站？），"
        "输入 v 用语音提问（英文），输入 exit 退出。\n"
    )

    while True:
        try:
            car_state = car_state_source.get_state()
            print_car_state(car_state)
            raw_input_text = input("\n玩家：").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见。")
            break

        if raw_input_text.lower() == "v":
            # Voice input is English-only (see voice_input.py) -- ENGINEER_PERSONA
            # always answers in English regardless of how the question was typed,
            # so this doesn't change the rest of the pipeline at all.
            user_question = voice_input.record_and_transcribe_blocking()
            if not user_question:
                print("[ChatEngineer] 没有识别到内容，请重试（或直接打字提问）。")
                continue
            print(f"[VoiceInput] 识别结果：{user_question}")
        else:
            user_question = raw_input_text

        if not user_question:
            continue
        if user_question.lower() in {"exit", "quit", "q"}:
            print("再见。")
            break

        overlay_broadcast.broadcast_engineer_start()
        try:
            answer = ask_engineer(user_question, car_state, base_url=MIDWARE_BASE_URL)
        except Exception as exc:
            print(f"[ChatEngineer] Middleware 请求失败：{exc}")
            overlay_broadcast.broadcast_engineer_error(str(exc))
            continue

        print(f"AI工程师：{answer}")
        overlay_broadcast.broadcast_engineer_reply(answer)


if __name__ == "__main__":
    main()
