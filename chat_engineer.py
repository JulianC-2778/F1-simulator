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
    TORCS_ENGINEER_BASE_URL          - Granite/LM Studio endpoint (see granite_client.py)
    TORCS_ENGINEER_MODEL             - model id override
    TORCS_ENGINEER_USE_FAKE_DATA     - "true" to force demo data instead of live telemetry
    TORCS_ENGINEER_UDP_PORT          - live telemetry UDP port, used only as a fallback
                                          when midware isn't reachable (default from config.py)
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
import granite_client
import overlay_broadcast
from car_state_source import (
    CarStateSource,
    FakeCarStateSource,
    HttpCarStateSource,
    LiveCarStateSource,
    wait_for_live_state,
)
from midware.context_manager import ContextConfig, ContextManager, ENGINEER_PERSONA, format_car_state
from telemetry_common import env_flag


USE_FAKE_DATA = env_flag("TORCS_ENGINEER_USE_FAKE_DATA", False)
MIDWARE_BASE_URL = os.getenv("TORCS_ENGINEER_MIDWARE_URL", config.MIDWARE_BASE_URL)
UDP_PORT = int(os.getenv("TORCS_ENGINEER_UDP_PORT", config.TELEMETRY_UDP_PORT))


def choose_car_state_source() -> CarStateSource:
    if USE_FAKE_DATA:
        print("[ChatEngineer] TORCS_ENGINEER_USE_FAKE_DATA=true -> using demo car_state data.")
        return FakeCarStateSource()

    # Probe midware's REST API first -- avoids the "Address already in use"
    # port conflict when midware (needed for the overlay window) is already
    # running. Only falls back to binding UDP directly if midware isn't
    # reachable, so this script still works standalone.
    midware_source = HttpCarStateSource(base_url=MIDWARE_BASE_URL)
    print(f"[ChatEngineer] Probing midware at {MIDWARE_BASE_URL} for live telemetry (no UDP bind)...")
    if wait_for_live_state(midware_source, timeout=2.0):
        print("[ChatEngineer] Live telemetry detected via midware, using real car_state data.")
        return midware_source

    print(
        f"[ChatEngineer] midware not reachable at {MIDWARE_BASE_URL} (or no telemetry yet); "
        f"falling back to binding UDP:{UDP_PORT} directly."
    )
    live = LiveCarStateSource(udp_port=UDP_PORT)
    if wait_for_live_state(live, timeout=5.0):
        print("[ChatEngineer] Live telemetry detected, using real car_state data.")
        return live

    print(
        "[ChatEngineer] No live telemetry yet. Falling back to demo data. "
        "Start TORCS with the human driver telemetry export enabled (see README), "
        "or set TORCS_ENGINEER_USE_FAKE_DATA=true to silence this message."
    )
    return FakeCarStateSource()


def print_car_state(car_state: dict[str, Any]) -> None:
    print("\n" + format_car_state(car_state))


def main() -> None:
    connection = granite_client.connect()
    granite_client.print_banner(connection)

    car_state_source = choose_car_state_source()
    ctx_mgr = ContextManager(ContextConfig(commentator_persona=ENGINEER_PERSONA))

    print("\n输入你的问题（例如：我的轮胎状态怎么样？/ 现在该不该进站？），输入 exit 退出。\n")

    while True:
        try:
            car_state = car_state_source.get_state()
            print_car_state(car_state)
            user_question = input("\n玩家：").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见。")
            break

        if not user_question:
            continue
        if user_question.lower() in {"exit", "quit", "q"}:
            print("再见。")
            break

        ctx_mgr.add_user(ctx_mgr.format_engineer_prompt(car_state, user_question))
        messages = ctx_mgr.build_messages()
        overlay_broadcast.broadcast_engineer_start()
        try:
            answer = granite_client.ask_engineer(connection, messages)
        except Exception as exc:
            print(f"[ChatEngineer] Granite 请求失败：{exc}")
            overlay_broadcast.broadcast_engineer_error(str(exc))
            continue

        print(f"AI工程师：{answer}")
        overlay_broadcast.broadcast_engineer_reply(answer)
        ctx_mgr.add_assistant(answer)


if __name__ == "__main__":
    main()
