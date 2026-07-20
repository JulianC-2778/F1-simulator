#!/usr/bin/env python3
"""
Feature 1: car_state data sources.

This file provides data sources for the chatbot side.

A module responsibility:
- race_analyzer.py converts raw TORCS telemetry into the agreed car_state format.
- car_state_source.py only chooses where the data comes from.

Supported sources:
- FakeCarStateSource: demo data, no TORCS needed.
- LiveCarStateSource: reads TORCS UDP telemetry directly.
- HttpCarStateSource: reads telemetry from midware/commentary.py REST API.
"""

from __future__ import annotations

import itertools
import json
import sys
import time
from pathlib import Path
from typing import Any, Protocol
from urllib import request

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from midware.telemetry import TelemetryStore, start_udp_listener
from race_analyzer import (
    CAR_STATE_KEYS,
    analyze_car_state,
    empty_car_state,
    telemetry_to_car_state,
    validate_car_state,
)


class CarStateSource(Protocol):
    def get_state(self) -> dict[str, Any]: ...


_DEMO_SCENARIOS: list[dict[str, Any]] = [
    {
        "speed": 210.0,
        "rpm": 8700.0,
        "gear": 5,
        "track_pos": 0.72,
        "damage": 1200.0,
        "fuel": 35.0,
        "lap_time": 102.3,
    },
    {
        "speed": 65.0,
        "rpm": 6200.0,
        "gear": 4,
        "track_pos": 0.15,
        "damage": 0.0,
        "fuel": 58.0,
        "lap_time": 41.2,
    },
    {
        "speed": 180.0,
        "rpm": 9100.0,
        "gear": 4,
        "track_pos": -0.95,
        "damage": 3400.0,
        "fuel": 12.0,
        "lap_time": 88.7,
    },
]


class FakeCarStateSource:
    """Demo data for testing without TORCS."""

    def __init__(self, scenarios: list[dict[str, Any]] | None = None) -> None:
        self._cycle = itertools.cycle(scenarios or _DEMO_SCENARIOS)

    def get_state(self) -> dict[str, Any]:
        raw = dict(next(self._cycle))
        raw["problems"] = analyze_car_state(raw)
        return validate_car_state(raw)


class LiveCarStateSource:
    """
    Read live TORCS UDP telemetry and return the agreed car_state dict.

    Uses midware.telemetry's shared parser/store (the same one midware/commentary.py
    uses) instead of a second, independent UDP-parsing implementation. Frames come
    back in midware's raw camelCase field names; telemetry_to_car_state() already
    accepts both that and the snake_case style, so no extra conversion is needed here.
    """

    def __init__(
        self,
        udp_port: int,
        retention_seconds: float = 3.0,
        *,
        standalone: bool = False,
    ) -> None:
        if not standalone:
            raise ValueError("Direct UDP telemetry requires explicit standalone=True")
        if udp_port == config.TELEMETRY_UDP_PORT:
            raise ValueError("Standalone tools must not bind the production telemetry port 3101")
        print(f"WARNING: standalone debug UDP listener enabled on non-production port {udp_port}")
        self._store = TelemetryStore(window_seconds=retention_seconds)
        start_udp_listener(self._store, port=udp_port)

    def is_ready(self) -> bool:
        return self._store.has_telemetry()

    def get_state(self) -> dict[str, Any]:
        frame, _rankings = self._store.latest()
        if not frame:
            return empty_car_state()

        return telemetry_to_car_state(frame)


class HttpCarStateSource:
    """
    Read live telemetry from midware/commentary.py instead of binding UDP directly.

    This avoids a port conflict when midware is already using the telemetry UDP port.
    Expected endpoint:
        GET {base_url}/api/telemetry

    Expected response shape:
        {
            "telemetry": {...},
            "rankings": [...]
        }
    """

    def __init__(self, base_url: str = config.MIDWARE_BASE_URL, timeout: float = 1.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _fetch_payload(self) -> dict[str, Any] | None:
        url = f"{self.base_url}/api/telemetry"
        req = request.Request(url, headers={"Accept": "application/json"}, method="GET")

        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw_text = response.read().decode("utf-8")
        except Exception:
            return None

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return None

        return payload if isinstance(payload, dict) else None

    def is_ready(self) -> bool:
        payload = self._fetch_payload()
        if not payload:
            return False

        telemetry = payload.get("telemetry")
        return isinstance(telemetry, dict) and bool(telemetry)

    def get_state(self) -> dict[str, Any]:
        payload = self._fetch_payload()
        if not payload:
            return empty_car_state()

        telemetry = payload.get("telemetry")
        if not isinstance(telemetry, dict) or not telemetry:
            return empty_car_state()

        return telemetry_to_car_state(telemetry)


def wait_for_live_state(source: Any, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout

    while time.time() < deadline:
        if hasattr(source, "is_ready") and source.is_ready():
            return True
        time.sleep(0.05)

    return False
