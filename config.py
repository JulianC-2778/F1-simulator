"""
Single source of truth for cross-cutting settings (ports, hosts) that are
shared across multiple scripts in this project.

Only values that were duplicated as independent literals in several files
live here -- that duplication is exactly what caused the mainline commentary
service's port to drift out of sync with its own callers after it changed
from 8765 to 8880 (several files kept the old default). Feature-specific
tuning parameters (timeouts, intervals, model temperature, etc.) that are
only ever read by one file are NOT moved here; they stay local.

Precedence for each setting: environment variable > config.json > the
hardcoded default below. This preserves every existing per-feature env var
override (e.g. TORCS_ENGINEER_MIDWARE_URL) -- those still take priority over
whatever this module resolves to; they just fall back to a single shared
value here instead of each duplicating their own literal.

Edit config.json to change a default for everyone at once. Environment
variables remain the way to override a single run/deployment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().with_name("config.json")


def _load_file() -> dict[str, Any]:
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_FILE = _load_file()


def _setting(env_name: str, file_key: str, default: Any) -> Any:
    env_value = os.getenv(env_name)
    if env_value is not None:
        return env_value
    return _FILE.get(file_key, default)


MIDWARE_HOST = str(_setting("TORCS_MIDWARE_HOST", "midware_host", "127.0.0.1"))
MIDWARE_PORT = int(_setting("TORCS_MIDWARE_PORT", "midware_port", 8880))
MIDWARE_BASE_URL = f"http://{MIDWARE_HOST}:{MIDWARE_PORT}"
MIDWARE_WS_URL = f"ws://{MIDWARE_HOST}:{MIDWARE_PORT}/ws"

TELEMETRY_UDP_PORT = int(_setting("TORCS_TELEMETRY_UDP_PORT", "telemetry_udp_port", 3101))
SCR_UDP_PORT = int(_setting("TORCS_SCR_UDP_PORT", "scr_udp_port", 3001))

FEATURE2_PORT = int(_setting("TORCS_FEATURE2_PORT", "feature2_port", 8766))
FEATURE2_BASE_URL = f"http://{MIDWARE_HOST}:{FEATURE2_PORT}"

TTS_PORT = int(_setting("TORCS_TTS_PORT", "tts_port", 8881))
TTS_BASE_URL = f"http://{MIDWARE_HOST}:{TTS_PORT}"
