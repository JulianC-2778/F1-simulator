from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from midware.feature2_core import build_dashboard_payload, empty_dashboard, overlay_prompt, pending_overlay, truncate_text  # noqa: E402
from telemetry_common import chat_completion_text, connect_openai_compatible_model, extract_json_object  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

COMMENTARY_BASE_URL = os.getenv("TORCS_FEATURE2_COMMENTARY_URL", "http://127.0.0.1:8765").rstrip("/")
FEATURE2_PORT = int(os.getenv("TORCS_FEATURE2_PORT", "8766"))
DEFAULT_WINDOW_SECONDS = float(os.getenv("TORCS_FEATURE2_WINDOW_SECONDS", "6.0"))
DEFAULT_HISTORY_SECONDS = float(os.getenv("TORCS_FEATURE2_HISTORY_SECONDS", "16.0"))
UPSTREAM_TIMEOUT_SECONDS = float(os.getenv("TORCS_FEATURE2_UPSTREAM_TIMEOUT", "4.0"))
OVERLAY_TIMEOUT_SECONDS = float(os.getenv("TORCS_FEATURE2_OVERLAY_TIMEOUT", "18.0"))
OVERLAY_CACHE_LIMIT = int(os.getenv("TORCS_FEATURE2_OVERLAY_CACHE_LIMIT", "48"))
OVERLAY_MAX_TOKENS = int(os.getenv("TORCS_FEATURE2_OVERLAY_MAX_TOKENS", "160"))
LIVE_FRAME_TIMEOUT_SECONDS = float(os.getenv("TORCS_FEATURE2_LIVE_TIMEOUT_SECONDS", "5.0"))

app = FastAPI(title="TORCS Feature 2 Standalone Service")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_overlay_cache: dict[str, dict[str, Any]] = {}
_overlay_tasks: dict[str, asyncio.Task[Any]] = {}
_model_connection: Any = None


def _trim_overlay_cache() -> None:
    while len(_overlay_cache) > OVERLAY_CACHE_LIMIT:
        oldest_key = next(iter(_overlay_cache))
        if oldest_key in _overlay_tasks:
            break
        _overlay_cache.pop(oldest_key, None)


def _get_model_connection() -> Any:
    global _model_connection
    if _model_connection is None:
        _model_connection = connect_openai_compatible_model()
    return _model_connection


def _request_overlay(payload: dict[str, Any]) -> str:
    connection = _get_model_connection()
    return chat_completion_text(
        connection,
        messages=[
            {
                "role": "system",
                "content": "You are a concise racing engineer assistant. Return stable JSON only.",
            },
            {
                "role": "user",
                "content": overlay_prompt(payload),
            },
        ],
        temperature=0.15,
        max_tokens=OVERLAY_MAX_TOKENS,
        timeout=OVERLAY_TIMEOUT_SECONDS,
    )


async def _generate_overlay(cache_key: str, payload: dict[str, Any]) -> None:
    try:
        text = await asyncio.to_thread(_request_overlay, payload)
        parsed = extract_json_object(text) or {}
        analysis = truncate_text(parsed.get("analysis") or text, 220)
        coach_note = truncate_text(parsed.get("coach_note"), 140)
        _overlay_cache[cache_key] = {
            "status": "ready",
            "source": "model_overlay",
            "analysis": analysis,
            "coach_note": coach_note,
            "updated_at": round(asyncio.get_running_loop().time(), 3),
            "error": "",
        }
    except Exception as exc:
        _overlay_cache[cache_key] = {
            "status": "error",
            "source": "model_overlay",
            "analysis": "",
            "coach_note": "",
            "updated_at": round(asyncio.get_running_loop().time(), 3),
            "error": truncate_text(str(exc), 180),
        }
    finally:
        _overlay_tasks.pop(cache_key, None)
        _trim_overlay_cache()


def _ensure_overlay(cache_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    overlay = _overlay_cache.get(cache_key)
    if overlay is None:
        overlay = pending_overlay()
        _overlay_cache[cache_key] = overlay

    if cache_key not in _overlay_tasks and overlay.get("status") not in {"ready", "error"}:
        _overlay_tasks[cache_key] = asyncio.create_task(_generate_overlay(cache_key, payload))

    return dict(overlay, cache_key=cache_key)


async def _fetch_upstream_frames(seconds: float) -> list[dict[str, Any]]:
    url = f"{COMMENTARY_BASE_URL}/api/telemetry/history"
    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
        response = await client.get(url, params={"seconds": seconds})
        response.raise_for_status()
        payload = response.json()
    frames = payload.get("frames", [])
    return frames if isinstance(frames, list) else []


def _frame_wall_time(frame: dict[str, Any]) -> float:
    try:
        return float(frame.get("_wall_time", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _latest_frame_age_seconds(frames: list[dict[str, Any]]) -> float | None:
    latest_wall_time = max((_frame_wall_time(frame) for frame in frames), default=0.0)
    if latest_wall_time <= 0.0:
        return None
    return max(0.0, time.time() - latest_wall_time)


def _latest_session_id(frames: list[dict[str, Any]]) -> int | None:
    if not frames:
        return None
    try:
        return int(float(frames[-1].get("_session_id", 0)))
    except (TypeError, ValueError):
        return None


async def _build_dashboard(window_seconds: float, history_seconds: float) -> dict[str, Any]:
    lookback_seconds = max(window_seconds, history_seconds)
    try:
        frames = await _fetch_upstream_frames(lookback_seconds)
    except Exception as exc:
        message = truncate_text(f"Upstream commentary service unavailable: {exc}", 220)
        return empty_dashboard(
            window_seconds,
            history_seconds,
            error=message,
            upstream_ok=False,
        )

    if not frames:
        return empty_dashboard(
            window_seconds,
            history_seconds,
            message="Waiting for a new telemetry session.",
            session_state="waiting",
        )

    data_age_seconds = _latest_frame_age_seconds(frames)
    session_id = _latest_session_id(frames)
    if data_age_seconds is not None and data_age_seconds > LIVE_FRAME_TIMEOUT_SECONDS:
        return empty_dashboard(
            window_seconds,
            history_seconds,
            message=f"No active session. Last telemetry arrived {data_age_seconds:.1f} s ago. Start a new run to repopulate the dashboard.",
            session_state="stale",
            session_id=session_id,
            data_age_seconds=round(data_age_seconds, 3),
        )

    dashboard = build_dashboard_payload(
        frames,
        window_seconds=window_seconds,
        history_seconds=history_seconds,
    )
    dashboard_status = dashboard.get("status") or {}
    dashboard_status["session_id"] = session_id
    dashboard_status["data_age_seconds"] = round(data_age_seconds or 0.0, 3)
    dashboard_status["message"] = "Telemetry live."
    dashboard["status"] = dashboard_status

    overlay_request = dashboard.pop("_overlay_request", None)
    overlay_cache_key = dashboard.pop("_overlay_cache_key", None)
    guidance = dashboard.get("guidance")
    if guidance is not None and overlay_request and overlay_cache_key:
        guidance["async_overlay"] = _ensure_overlay(overlay_cache_key, overlay_request)
    return dashboard


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = STATIC_DIR / "feature2.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>feature2.html not found</h1>", status_code=404)


@app.get("/feature2", response_class=HTMLResponse)
async def feature2_page() -> HTMLResponse:
    return await index()


@app.get("/api/feature2/dashboard")
async def feature2_dashboard(
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    history_seconds: float = DEFAULT_HISTORY_SECONDS,
) -> dict[str, Any]:
    return await _build_dashboard(window_seconds=window_seconds, history_seconds=history_seconds)


@app.get("/api/feature2/health")
async def feature2_health() -> dict[str, Any]:
    try:
        frames = await _fetch_upstream_frames(DEFAULT_WINDOW_SECONDS)
        return {
            "ok": True,
            "commentary_base_url": COMMENTARY_BASE_URL,
            "frame_count": len(frames),
        }
    except Exception as exc:
        return {
            "ok": False,
            "commentary_base_url": COMMENTARY_BASE_URL,
            "error": truncate_text(str(exc), 220),
        }


if __name__ == "__main__":
    log.info("Feature 2 standalone service -> http://0.0.0.0:%s", FEATURE2_PORT)
    uvicorn.run(app, host="0.0.0.0", port=FEATURE2_PORT, reload=False)
