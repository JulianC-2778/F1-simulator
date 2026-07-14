from __future__ import annotations

from typing import Any


FEATURE_SPECS: list[dict[str, Any]] = [
    {
        "name": "commentary",
        "label": "Live Commentary",
        "description": "Real-time event detection, AI commentary, subtitles, and optional TTS.",
        "entrypoint": "midware/commentary.py",
        "can_start": True,
        "can_stop": False,
    },
    {
        "name": "engineer",
        "label": "AI Race Engineer",
        "description": "Question answering over live car state and detected driving issues.",
        "entrypoint": "chat_engineer.py",
        "can_start": False,
        "can_stop": False,
    },
    {
        "name": "coach",
        "label": "Telemetry Coach",
        "description": "Rule-based and model-assisted telemetry dashboard guidance.",
        "entrypoint": "midware/feature2_service.py",
        "can_start": False,
        "can_stop": False,
    },
    {
        "name": "bot",
        "label": "AI Driver Bot",
        "description": "SCR-based AI driving client status and strategy monitoring.",
        "entrypoint": "ai_bot.py",
        "can_start": False,
        "can_stop": False,
    },
]


def feature_specs() -> list[dict[str, Any]]:
    return [dict(item) for item in FEATURE_SPECS]


def feature_status(
    *,
    has_telemetry: bool,
    commentary_mode: str,
    commentary_task_running: bool,
    ws_client_count: int,
    tts_enabled: bool,
    coach_available: bool,
    model_base_url: str,
    model_name: str,
) -> list[dict[str, Any]]:
    return [
        {
            "name": "commentary",
            "enabled": commentary_mode != "off",
            "running": True,
            "healthy": True,
            "last_error": "",
            "last_update": 0,
            "details": {
                "mode": commentary_mode,
                "has_telemetry": has_telemetry,
                "active_generation": commentary_task_running,
                "ws_clients": ws_client_count,
                "tts_enabled": tts_enabled,
            },
        },
        {
            "name": "engineer",
            "enabled": True,
            "running": False,
            "healthy": True,
            "last_error": "",
            "last_update": 0,
            "details": {
                "integration": "legacy_cli_gui",
                "model_base_url": model_base_url,
                "model": model_name,
            },
        },
        {
            "name": "coach",
            "enabled": True,
            "running": coach_available,
            "healthy": coach_available,
            "last_error": "" if coach_available else "No telemetry frames available yet.",
            "last_update": 0,
            "details": {
                "api": "/api/coach/dashboard",
                "legacy_api": "/api/feature2/dashboard on the standalone service",
            },
        },
        {
            "name": "bot",
            "enabled": False,
            "running": False,
            "healthy": True,
            "last_error": "",
            "last_update": 0,
            "details": {
                "integration": "planned_status_adapter",
                "api": "planned /api/bot/status",
            },
        },
    ]

