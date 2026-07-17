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

import config  # noqa: E402
from midware.feature2_core import build_dashboard_payload, empty_dashboard, empty_track_context, overlay_prompt, pending_overlay, truncate_text  # noqa: E402
from midware.telemetry import to_common_frame  # noqa: E402
from telemetry_common import chat_completion_text, connect_openai_compatible_model, extract_json_object  # noqa: E402
from track_model import load_track_model  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

COMMENTARY_BASE_URL = os.getenv("TORCS_FEATURE2_COMMENTARY_URL", config.MIDWARE_BASE_URL).rstrip("/")
FEATURE2_PORT = int(os.getenv("TORCS_FEATURE2_PORT", config.FEATURE2_PORT))
DEFAULT_WINDOW_SECONDS = float(os.getenv("TORCS_FEATURE2_WINDOW_SECONDS", "6.0"))
DEFAULT_HISTORY_SECONDS = float(os.getenv("TORCS_FEATURE2_HISTORY_SECONDS", "16.0"))
UPSTREAM_TIMEOUT_SECONDS = float(os.getenv("TORCS_FEATURE2_UPSTREAM_TIMEOUT", "4.0"))
OVERLAY_TIMEOUT_SECONDS = float(os.getenv("TORCS_FEATURE2_OVERLAY_TIMEOUT", "60.0"))
OVERLAY_ERROR_RETRY_SECONDS = float(os.getenv("TORCS_FEATURE2_OVERLAY_ERROR_RETRY_SECONDS", "20.0"))
OVERLAY_CACHE_LIMIT = int(os.getenv("TORCS_FEATURE2_OVERLAY_CACHE_LIMIT", "48"))
OVERLAY_MAX_TOKENS = int(os.getenv("TORCS_FEATURE2_OVERLAY_MAX_TOKENS", "160"))
LIVE_FRAME_TIMEOUT_SECONDS = float(os.getenv("TORCS_FEATURE2_LIVE_TIMEOUT_SECONDS", "5.0"))
TRACK_MODEL_SPEC = os.getenv("TORCS_FEATURE2_TRACK_MODEL", "auto").strip()
TRACK_MODEL_RETRY_SECONDS = float(os.getenv("TORCS_FEATURE2_TRACK_MODEL_RETRY_SECONDS", "10.0"))

app = FastAPI(title="TORCS Feature 2 Standalone Service")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_overlay_cache: dict[str, dict[str, Any]] = {}
_overlay_tasks: dict[str, asyncio.Task[Any]] = {}
_model_connection: Any = None
_track_model: Any = None
_track_model_attempted = False
_track_model_last_attempt = 0.0
_track_model_error = ""


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


def _track_model_enabled() -> bool:
    return TRACK_MODEL_SPEC.lower() not in {"", "off", "none", "disabled", "false", "0"}


def _get_track_model() -> Any:
    global _track_model, _track_model_attempted, _track_model_error, _track_model_last_attempt
    if not _track_model_enabled():
        _track_model_error = "Track model disabled by TORCS_FEATURE2_TRACK_MODEL."
        return None
    now = time.monotonic()
    should_retry = (
        _track_model is None
        and _track_model_attempted
        and now - _track_model_last_attempt >= TRACK_MODEL_RETRY_SECONDS
    )
    if not _track_model_attempted or should_retry:
        _track_model_attempted = True
        _track_model_last_attempt = now
        try:
            _track_model = load_track_model(TRACK_MODEL_SPEC or "auto", quiet=True)
            _track_model_error = "" if _track_model is not None else "Track model could not be loaded."
        except Exception as exc:  # noqa: BLE001 - optional context must never block feature 2.
            _track_model = None
            _track_model_error = truncate_text(str(exc), 180)
    return _track_model


def _build_track_context(frames: list[dict[str, Any]]) -> dict[str, Any]:
    model = _get_track_model()
    if model is None:
        return empty_track_context(_track_model_error)
    if not frames:
        return empty_track_context("No telemetry frame available for track model lookup.")

    latest = to_common_frame(frames[-1])
    dist_from_start = float(latest.get("dist_from_start", 0.0))
    speed_x = float(latest.get("speed_x", 0.0))
    limit_kmh = float(model.limit_kmh(dist_from_start))
    line_tpos = float(model.line_tpos(dist_from_start))
    if line_tpos > 0.1:
        line_hint = "left side"
    elif line_tpos < -0.1:
        line_hint = "right side"
    else:
        line_hint = "center"

    next_corner = model.next_corner(dist_from_start)
    return {
        "available": True,
        "source": "track_model",
        "name": getattr(model, "name", ""),
        "summary": model.summary(),
        "dist_from_start": round(dist_from_start, 3),
        "limit_kmh": round(limit_kmh, 1),
        "speed_over_limit": round(speed_x - limit_kmh, 1),
        "line_tpos": round(line_tpos, 3),
        "line_hint": line_hint,
        "next_corner": next_corner,
        "error": "",
    }


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
            "braking_tip": truncate_text(parsed.get("braking_tip"), 140),
            "cornering_tip": truncate_text(parsed.get("cornering_tip"), 140),
            "throttle_tip": truncate_text(parsed.get("throttle_tip"), 140),
            "updated_at": round(asyncio.get_running_loop().time(), 3),
            "error": "",
        }
    except Exception as exc:
        _overlay_cache[cache_key] = {
            "status": "error",
            "source": "model_overlay",
            "analysis": "",
            "coach_note": "",
            "braking_tip": "",
            "cornering_tip": "",
            "throttle_tip": "",
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
    elif overlay.get("status") == "error":
        now = asyncio.get_running_loop().time()
        updated_at = float(overlay.get("updated_at") or 0.0)
        if now - updated_at >= OVERLAY_ERROR_RETRY_SECONDS:
            overlay = pending_overlay()
            _overlay_cache[cache_key] = overlay

    if cache_key not in _overlay_tasks and overlay.get("status") not in {"ready", "error"}:
        _overlay_tasks[cache_key] = asyncio.create_task(_generate_overlay(cache_key, payload))

    return dict(overlay, cache_key=cache_key)


async def _fetch_upstream_frames(seconds: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url = f"{COMMENTARY_BASE_URL}/api/telemetry/history"
    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
        response = await client.get(url, params={"seconds": seconds})
        response.raise_for_status()
        payload = response.json()
    frames = payload.get("frames", [])
    status = payload.get("status", {})
    return frames if isinstance(frames, list) else [], status if isinstance(status, dict) else {}


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
        frames, upstream_status = await _fetch_upstream_frames(lookback_seconds)
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
        track_context=_build_track_context(frames),
    )
    dashboard["status"].update(
        {
            "telemetry_packet_count": upstream_status.get("packet_count"),
            "last_received_at": upstream_status.get("last_received_at"),
            "last_progress_at": upstream_status.get("last_progress_at"),
            "seconds_since_received": upstream_status.get("seconds_since_received"),
            "seconds_since_progress": upstream_status.get("seconds_since_progress"),
            "is_stale": bool(upstream_status.get("is_stale")),
            "stale_reason": upstream_status.get("stale_reason", ""),
            "track_model_enabled": _track_model_enabled(),
            "track_model_spec": TRACK_MODEL_SPEC,
            "track_model_available": bool(dashboard.get("track_model", {}).get("available")),
            "track_model_name": dashboard.get("track_model", {}).get("name", ""),
            "track_model_error": dashboard.get("track_model", {}).get("error", ""),
        }
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
        frames, upstream_status = await _fetch_upstream_frames(DEFAULT_WINDOW_SECONDS)
        model = _get_track_model()
        return {
            "ok": True,
            "commentary_base_url": COMMENTARY_BASE_URL,
            "frame_count": len(frames),
            "telemetry_status": upstream_status,
            "track_model": {
                "enabled": _track_model_enabled(),
                "spec": TRACK_MODEL_SPEC,
                "available": model is not None,
                "name": getattr(model, "name", "") if model is not None else "",
                "error": _track_model_error,
            },
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
