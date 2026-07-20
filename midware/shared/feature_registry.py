from __future__ import annotations

from typing import Any


FEATURE_SPECS: list[dict[str, Any]] = [
    {
        "name": "commentary",
        "label": "Live Commentary",
        "description": "Real-time event detection, AI commentary, subtitles, and optional TTS.",
        "entrypoint": "python3 -m midware.app",
    },
    {
        "name": "engineer",
        "label": "AI Race Engineer",
        "description": "Question answering over live car state and detected driving issues.",
        "entrypoint": "POST /api/engineer/ask",
    },
    {
        "name": "coach",
        "label": "Telemetry Coach",
        "description": "Rule-based and model-assisted telemetry dashboard guidance.",
        "entrypoint": "GET /api/coach/dashboard",
    },
    {
        "name": "bot",
        "label": "AI Driver Bot",
        "description": "SCR-based AI driving client status and strategy monitoring.",
        "entrypoint": "ai_bot.py --bot",
    },
]


def feature_specs() -> list[dict[str, Any]]:
    return [dict(item) for item in FEATURE_SPECS]

