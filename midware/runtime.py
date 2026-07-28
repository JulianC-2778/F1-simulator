"""
TORCS 比赛解说中间件 — 内部运行装配（兼容实现，逐步拆分中）

功能：
  · REST API：AI 配置 / Context 配置 / 手动触发解说
  · WebSocket：实时推送解说流 & TORCS 数据
  · UDP 监听器（后台线程）：接收 TORCS human 模块推送的遥测数据（端口 3101）
  · CSV 文件读取：从历史 CSV 回放生成解说

启动：
    pip install fastapi uvicorn httpx aiofiles
    python server.py
"""

import asyncio
import base64
import csv
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT_DIR = Path(__file__).resolve().parent.parent
MIDWARE_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(MIDWARE_DIR) not in sys.path:
    sys.path.insert(0, str(MIDWARE_DIR))

import config
from telemetry_common import extract_json_object
from commentary_engine import CommentaryConfig, CommentaryEngine
from context_manager import ContextConfig, ContextManager, ENGINEER_PERSONA
from midware.shared.feature_registry import feature_specs
from midware.shared.model_gateway import OpenAICompatibleGateway
from midware.services.model_broker import MODEL_PRIORITIES, ModelBroker
from midware.schemas.bot import BotStrategyRequest, Strategy, StrategyDecision
from midware.schemas.bot import BotStatusUpdate
from midware.services.bot_status_service import BotStatusService
from midware.services.process_manager import ProcessRegistry
from midware.shared.output_bus import ALLOWED_SOURCES, normalize_outbound_message
from midware.shared.race_snapshot import build_race_snapshot
from midware.services.feature_gate import FeatureGate
from midware.feature2_core import (
    build_dashboard_payload,
    build_lookahead_plan,
    build_pre_race_briefing,
    driver_style_profile,
    prebrief_prompt,
    road_condition_profile,
)
from race_analyzer import empty_car_state, telemetry_to_car_state
import voice_input
from midware.services.telemetry_service import TelemetryService
from midware.shared.track_profiles import coach_track_context, list_track_profiles
from midware.telemetry import to_common_frame

# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
UI_FILE = "index.html"  # 可被启动参数覆盖

# -- AI API config (editable from UI) --
api_config: dict[str, Any] = {
    "base_url":    "http://localhost:1234/v1",
    "api_key":     "",
    "model":       "ibm-granite",
    "temperature": 0.8,
    "stream":      True,
}

# -- TTS config --
tts_config: dict[str, Any] = {
    "enabled": False,
    "provider": "kokoro",
    "url":     f"{config.TTS_BASE_URL}/tts",
    "api_key": "",
    "model":   "",
    "voice":   "af_heart",
    "speed":   1.2,
    "volume":  1.0,
    "response_format": "audio_bytes",
    "response_audio_field": "audio",
    "mime": "audio/wav",
    "request_template": json.dumps({
        "text": "{text}",
        "model": "{model}",
        "voice": "{voice}",
        "speed": "{speed}",
        "volume": "{volume}",
    }, indent=2),
}

# -- Context 配置 --
ctx_cfg   = ContextConfig()
ctx_mgr   = ContextManager(ctx_cfg)
engineer_ctx_mgr = ContextManager(ContextConfig(commentator_persona=ENGINEER_PERSONA))

# -- Engineer voice input (server-side mic recording, same mechanism as
# chat_engineer_gui.py's mic button -- see voice_input.py). Single global
# recorder because only one dashboard operator is expected to be recording
# at a time; a second start attempt while one is in progress is rejected
# rather than silently dropping the first recording.
_engineer_voice_recorder: "voice_input.Recorder | None" = None

# -- 遥测数据缓存（UDP 线程写入，主线程读） --
telemetry_service = TelemetryService(port=config.TELEMETRY_UDP_PORT, window_seconds=30.0)
telemetry_store = telemetry_service.store

# -- WebSocket 客户端集合 --
ws_clients: set[WebSocket] = set()

# -- 自动解说配置 --
commentary_engine = CommentaryEngine(
    CommentaryConfig(
        mode="hybrid",
        baseline_interval=10.0,
        event_cooldown=1.0,
        window_seconds=6.0,
        dedupe_seconds=10.0,
        max_words=45,
    )
)
_auto_task: asyncio.Task | None = None
_commentary_task: asyncio.Task | None = None
_commentary_priority: int = 0
runtime_manager = FeatureGate(feature_specs())
model_broker = ModelBroker(max_queue_size=16, max_concurrency=1)
bot_status_service = BotStatusService()

# -- Dashboard-controlled subprocesses (currently just the AI bot client).
# This does not replace launcher_gui.py; it is a separate, HTTP-reachable
# copy of the same start/stop/tail pattern for the unified web dashboard.
process_registry = ProcessRegistry()
process_registry.register(
    "ai_bot",
    "AI Driver Bot (ai_bot.py --bot --granite)",
    [sys.executable, "ai_bot.py", "--bot", "--granite"],
    ROOT_DIR,
)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="TORCS 比赛解说中间件")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# 广播消息给所有 WebSocket 客户端
# ---------------------------------------------------------------------------

async def broadcast(msg: dict):
    msg = normalize_outbound_message(msg)
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


async def broadcast_config_updated(section: str):
    await broadcast({"type": "config_updated", "section": section})


# ---------------------------------------------------------------------------
# AI 调用
# ---------------------------------------------------------------------------

def _tts_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = str(tts_config.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _render_tts_template(text: str) -> dict[str, Any]:
    template = str(tts_config.get("request_template") or "").strip()
    if not template:
        template = json.dumps({"text": "{text}"})

    values = {
        "text": text,
        "model": tts_config.get("model", ""),
        "voice": tts_config.get("voice", ""),
        "speed": tts_config.get("speed", 1.0),
        "volume": tts_config.get("volume", 1.0),
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f'"{{{key}}}"', json.dumps(value))
        rendered = rendered.replace(f"{{{key}}}", json.dumps(value))

    return json.loads(rendered)


def _extract_tts_audio(resp: httpx.Response) -> bytes | None:
    response_format = tts_config.get("response_format", "audio_bytes")
    if response_format == "audio_bytes":
        return resp.content

    data = resp.json()
    if response_format == "json_base64":
        field = str(tts_config.get("response_audio_field") or "audio")
        audio = data
        for part in field.split("."):
            audio = audio[part]
        if isinstance(audio, str) and "," in audio and audio.startswith("data:"):
            audio = audio.split(",", 1)[1]
        return base64.b64decode(audio)

    log.warning(f"Unsupported TTS response_format: {response_format}")
    return None


async def call_tts(text: str) -> bytes | None:
    if not tts_config["enabled"] or not text.strip():
        return None
    provider = tts_config.get("provider") or "kokoro"
    if provider in {"browser", "off"}:
        return None

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if provider == "kokoro":
                resp = await client.post(tts_config["url"], json={
                    "text": text,
                    "voice": tts_config["voice"],
                    "speed": tts_config["speed"],
                    "volume": tts_config["volume"],
                })
            elif provider == "custom_http":
                resp = await client.post(
                    tts_config["url"],
                    headers=_tts_headers(),
                    json=_render_tts_template(text),
                )
            else:
                log.warning(f"Unsupported TTS provider: {provider}")
                return None

            if resp.status_code == 200:
                return _extract_tts_audio(resp)
            log.warning(f"TTS server returned {resp.status_code}")
    except Exception as e:
        log.warning(f"TTS call failed: {e}")
    return None


async def call_ai(
    messages: list[dict],
    *,
    task: str,
    priority: int,
    stale_key: str | None = None,
    request_id: str,
) -> str:
    """Call the OpenAI-compatible API and return full reply text, streaming tokens via WebSocket."""
    key      = api_config["api_key"]
    base_url = api_config["base_url"].rstrip("/")
    model    = api_config["model"]
    temp     = api_config["temperature"]
    do_stream = api_config["stream"]

    gateway = OpenAICompatibleGateway(
        base_url=base_url,
        api_key=key,
        model=model,
        temperature=temp,
        max_tokens=ctx_cfg.max_response_tokens,
        stream=do_stream,
        timeout=60,
    )

    async def on_token(token: str) -> None:
        await broadcast({"type": "token", "text": token, "source": "commentary", "request_id": request_id})

    full_text = await model_broker.submit(
        lambda: gateway.chat(messages, on_token=on_token if do_stream else None),
        feature="commentary",
        task=task,
        priority=priority,
        timeout_s=75,
        stale_key=stale_key,
    )
    if not do_stream:
        await broadcast({"type": "token", "text": full_text, "source": "commentary", "request_id": request_id})
    return full_text


async def call_model_for_feature(
    messages: list[dict[str, Any]],
    *,
    source: str,
    task: str,
    priority: int,
    temperature: float | None = None,
    max_tokens: int | None = None,
    stream: bool = False,
    timeout: float = 45,
    request_id: str | None = None,
) -> str:
    gateway = OpenAICompatibleGateway(
        base_url=str(api_config["base_url"]).rstrip("/"),
        api_key=str(api_config.get("api_key") or ""),
        model=str(api_config.get("model") or "ibm-granite"),
        temperature=float(api_config["temperature"] if temperature is None else temperature),
        max_tokens=int(ctx_cfg.max_response_tokens if max_tokens is None else max_tokens),
        stream=stream,
        timeout=timeout,
    )

    async def on_token(token: str) -> None:
        await broadcast({"type": "token", "text": token, "source": source, "request_id": request_id})

    stale_key = "bot:default" if task == "bot_strategy" else None
    return await model_broker.submit(
        lambda: gateway.chat(messages, on_token=on_token if stream else None),
        feature=source,
        task=task,
        priority=priority,
        timeout_s=timeout + 15,
        stale_key=stale_key,
    )


def _extract_stream_token(line: str) -> str:
    """Extract a single token from an SSE data line."""
    if not line.startswith("data:"):
        return ""
    chunk = line[5:].strip()
    if chunk in ("[DONE]", ""):
        return ""
    try:
        data = json.loads(chunk)
        return data["choices"][0].get("delta", {}).get("content", "") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 解说生成（核心流程）
# ---------------------------------------------------------------------------

async def generate_commentary(
    telemetry: dict | None = None,
    rankings: list | None = None,
    manual_prompt: str | None = None,
    event_payload: dict | None = None,
    history_mode: str = "full",
) -> str:
    """
    构建上下文 → 调用 AI → 存入历史 → 广播。
    """
    request_id = str(uuid.uuid4())
    # 1. Build user message
    if event_payload:
        user_content = ctx_mgr.format_event_prompt(event_payload)
        history_content = ctx_mgr.format_event_history_entry(event_payload)
    elif manual_prompt:
        user_content = manual_prompt
        history_content = user_content
    elif telemetry:
        user_content = ctx_mgr.format_telemetry(telemetry, rankings)
        history_content = user_content
    else:
        raise ValueError("没有遥测数据或手动 prompt")

    # 2. 加入历史
    if history_mode != "assistant_only":
        ctx_mgr.add_user(history_content)

    # 3. 广播 user 消息（用于 UI 显示）
    await broadcast({
        "type": "user_msg",
        "source": "commentary",
        "request_id": request_id,
        "content": user_content,
        "stats": ctx_mgr.stats(),
    })

    # 4. 构建发送给 AI 的消息列表（已裁剪）
    messages = ctx_mgr.build_messages()

    # 5. 调用 AI
    await broadcast({"type": "ai_start", "source": "commentary", "request_id": request_id})
    try:
        if event_payload:
            model_broker.invalidate("commentary_baseline")
            task = "commentary_event"
            priority = MODEL_PRIORITIES["commentary_event"]
            stale_key = None
        elif manual_prompt:
            task = "commentary_manual"
            priority = MODEL_PRIORITIES["commentary_manual"]
            stale_key = None
        else:
            task = "commentary_baseline"
            priority = MODEL_PRIORITIES["commentary_baseline"]
            stale_key = "commentary_baseline"
        reply = await call_ai(
            messages,
            task=task,
            priority=priority,
            stale_key=stale_key,
            request_id=request_id,
        )
    except Exception as e:
        await broadcast({"type": "error", "source": "commentary", "message": str(e), "request_id": request_id})
        raise

    # 6. 把 AI 回复存入历史
    ctx_mgr.add_assistant(reply)

    # 7. TTS — 先合成语音，再广播字幕，确保字幕和音频始终一起出现。
    # 如果这期间被新事件取消（见 _auto_commentary_loop 的抢占逻辑），
    # 这条解说会连字幕带音频一起被丢弃，不会出现"有字没声"的半吊子状态。
    audio = await call_tts(reply)

    # 8. 广播字幕 + 音频（一起提交，用 shield 保护不被取消打断）
    async def _commit():
        await broadcast({
            "type": "ai_done",
            "source": "commentary",
            "request_id": request_id,
            "content": reply,
            "stats": ctx_mgr.stats(),
        })
        if audio:
            await broadcast({
                "type": "tts_audio",
                "source": "commentary",
                "request_id": request_id,
                "audio": base64.b64encode(audio).decode(),
                "mime":  tts_config.get("mime") or "audio/wav",
            })

    await asyncio.shield(asyncio.create_task(_commit()))

    return reply


# ---------------------------------------------------------------------------
# 自动解说定时任务
# ---------------------------------------------------------------------------

async def _run_commentary(decision, t, r):
    try:
        reply = await generate_commentary(t, r, event_payload=decision.payload, history_mode="summary")
        if not commentary_engine.should_emit_text(reply, float(decision.payload.get("event_time", 0.0))):
            log.info("重复解说已被去重记录标记")
    except asyncio.CancelledError:
        log.info(f"解说被新事件中断: {decision.event.get('event_type')}")
        raise
    except Exception as e:
        log.warning(f"自动解说失败: {e}")


async def _auto_commentary_loop():
    global _commentary_task, _commentary_priority
    while True:
        if not runtime_manager.is_enabled("commentary"):
            await asyncio.sleep(0.5)
            continue
        cfg = commentary_engine.config
        if cfg.mode == "off":
            await asyncio.sleep(1)
            continue

        await asyncio.sleep(0.5)

        t, r = telemetry_store.latest()
        if t is None:
            continue

        try:
            frames = telemetry_store.recent_frames(cfg.window_seconds)
            decision = commentary_engine.next_decision(frames, r)
            if decision is None:
                continue

            new_priority = decision.event.get("priority", 0)

            if _commentary_task and not _commentary_task.done():
                if new_priority >= _commentary_priority:
                    _commentary_task.cancel()
                    await asyncio.sleep(0)
                else:
                    continue

            await broadcast({"type": "event_detected", "event": decision.event, "payload": decision.payload})
            _commentary_priority = new_priority
            _commentary_task = asyncio.create_task(_run_commentary(decision, t, r))

        except Exception as e:
            log.warning(f"自动解说失败: {e}")


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / UI_FILE
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse(f"<h1>找不到 {UI_FILE}，请检查 static/ 目录</h1>")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Unified web dashboard: Commentary/Engineer/Coach tabs + AI bot control
    card, all driven by the shared APIs in docs/integration-contract.md.
    Additive only -- does not replace `/` (index.html / index2.html) or
    `/feature2` (feature2_service.py), which keep working unchanged."""
    html_path = STATIC_DIR / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>找不到 dashboard.html，请检查 midware/static/ 目录</h1>")


@app.get("/api/config")
async def get_config():
    return {
        "api": {**api_config, "api_key": "***" if api_config["api_key"] else ""},
        "tts": tts_config,
        "context": {
            "max_context_tokens":  ctx_cfg.max_context_tokens,
            "max_response_tokens": ctx_cfg.max_response_tokens,
            "trim_strategy":       ctx_cfg.trim_strategy,
            "chat_template":       ctx_cfg.chat_template,
            "commentator_persona": ctx_cfg.commentator_persona,
            "included_fields":     ctx_cfg.included_fields,
            "include_rankings":    ctx_cfg.include_rankings,
        },
        "auto_interval": commentary_engine.config.baseline_interval if commentary_engine.config.mode != "off" else 0,
        "commentary": {
            "mode": commentary_engine.config.mode,
            "baseline_interval": commentary_engine.config.baseline_interval,
            "event_cooldown": commentary_engine.config.event_cooldown,
            "window_seconds": commentary_engine.config.window_seconds,
            "dedupe_seconds": commentary_engine.config.dedupe_seconds,
            "max_words": commentary_engine.config.max_words,
        },
    }


@app.get("/api/features")
async def get_features():
    return {
        "features": runtime_manager.features(),
        "combination": runtime_manager.combination(),
    }


@app.get("/api/features/status")
async def get_features_status():
    _refresh_runtime_status()
    return {"features": runtime_manager.status(), "combination": runtime_manager.combination()}


@app.post("/api/features/enabled")
async def update_enabled_features(body: dict):
    try:
        enabled = body.get("enabled", [])
        if not isinstance(enabled, list):
            raise ValueError("enabled must be a list of feature names")
        runtime_manager.set_enabled([str(name) for name in enabled])
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    _refresh_runtime_status()
    await broadcast({"type": "config_updated", "section": "features", "source": "system"})
    return {"ok": True, "features": runtime_manager.status(), "combination": runtime_manager.combination()}


@app.get("/api/health")
async def get_health():
    telemetry, rankings = telemetry_store.latest()
    telemetry_status = telemetry_store.status()
    _refresh_runtime_status()
    scheduler = model_broker.status()
    has_telemetry = telemetry is not None
    return {
        "ok": has_telemetry and scheduler["failed"] == 0,
        "midware": True,
        "telemetry": {
            "ok": has_telemetry,
            "port": config.TELEMETRY_UDP_PORT,
            "status": telemetry_status,
            "ingestor": telemetry_service.ingestor.status(),
            "snapshot": build_race_snapshot(telemetry, rankings),
        },
        "model": {
            "base_url": api_config.get("base_url"),
            "model": api_config.get("model"),
            "scheduler": scheduler,
        },
        "tts": {
            "enabled": bool(tts_config.get("enabled")),
            "url": tts_config.get("url"),
        },
        "overlay": {
            "ws_clients": len(ws_clients),
        },
        "features": runtime_manager.status(),
        "combination": runtime_manager.combination(),
    }


@app.post("/api/config/tts")
async def update_tts_config(body: dict):
    for k in (
        "enabled",
        "provider",
        "url",
        "api_key",
        "model",
        "voice",
        "speed",
        "volume",
        "response_format",
        "response_audio_field",
        "mime",
        "request_template",
    ):
        if k in body:
            tts_config[k] = body[k]
    await broadcast_config_updated("tts")
    return {"ok": True, "tts": tts_config}


@app.post("/api/config/api")
async def update_api_config(body: dict):
    for k in ("base_url", "api_key", "model", "temperature", "stream"):
        if k in body:
            api_config[k] = body[k]
    await broadcast_config_updated("api")
    return {"ok": True}


@app.post("/api/config/context")
async def update_context_config(body: dict):
    """更新上下文配置。"""
    global ctx_cfg, ctx_mgr
    for k, v in body.items():
        if hasattr(ctx_cfg, k):
            setattr(ctx_cfg, k, v)
    ctx_mgr.config = ctx_cfg
    await broadcast_config_updated("context")
    return {"ok": True, "stats": ctx_mgr.stats()}


@app.post("/api/config/auto_interval")
async def update_auto_interval(body: dict):
    interval = float(body.get("interval", 0))
    commentary_engine.config.baseline_interval = interval
    commentary_engine.config.mode = "interval" if interval > 0 else "off"
    await broadcast_config_updated("commentary")
    return {"ok": True, "interval": interval, "mode": commentary_engine.config.mode}


@app.get("/api/commentary/config")
async def get_commentary_config():
    return {
        "mode": commentary_engine.config.mode,
        "baseline_interval": commentary_engine.config.baseline_interval,
        "event_cooldown": commentary_engine.config.event_cooldown,
        "window_seconds": commentary_engine.config.window_seconds,
        "dedupe_seconds": commentary_engine.config.dedupe_seconds,
        "max_words": commentary_engine.config.max_words,
    }


@app.post("/api/commentary/config")
async def update_commentary_config(body: dict):
    commentary_engine.update_config(body)
    config = await get_commentary_config()
    await broadcast_config_updated("commentary")
    return {"ok": True, "config": config}


@app.post("/api/commentary/manual")
async def manual_commentary(body: dict):
    """手动触发一次解说（可附带自定义 prompt）。"""
    if not runtime_manager.is_enabled("commentary"):
        return JSONResponse({"ok": False, "error": "commentary feature is disabled"}, status_code=409)
    t, r = telemetry_store.latest()

    prompt = body.get("prompt") or None
    asyncio.create_task(generate_commentary(t, r, manual_prompt=prompt))
    return {"ok": True, "queued": True}


@app.post("/api/commentary/clear")
async def clear_history():
    ctx_mgr.clear_history()
    return {"ok": True, "stats": ctx_mgr.stats()}


@app.get("/api/telemetry")
async def get_telemetry():
    telemetry, rankings = telemetry_store.latest()
    return {"telemetry": telemetry, "rankings": rankings}


@app.get("/api/race/snapshot")
async def get_race_snapshot():
    telemetry, rankings = telemetry_store.latest()
    return {"snapshot": build_race_snapshot(telemetry, rankings)}


@app.get("/api/telemetry/history")
async def get_telemetry_history(seconds: float | None = None):
    return {
        "frames": telemetry_store.recent_frames(seconds),
        "rankings": telemetry_store.recent_rankings(seconds),
        "status": telemetry_store.status(),
    }


def _frame_float(frame: dict[str, Any] | None, *keys: str, default: float = 0.0) -> float:
    if not frame:
        return default
    for key in keys:
        try:
            value = frame.get(key)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coach_track_context(
    frames: list[dict[str, Any]],
    *,
    track_id: str | None = None,
    road_condition: str | None = None,
    profile_override: dict[str, Any] | None = None,
    dist_from_start: float | None = None,
    speed_kmh: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    latest = frames[-1] if frames else None
    dist = float(dist_from_start) if dist_from_start is not None else _frame_float(latest, "distFromStart", "dist_from_start")
    speed = float(speed_kmh) if speed_kmh is not None else _frame_float(latest, "speedX", "speed_x")
    return coach_track_context(
        track_id=track_id,
        dist_from_start=dist,
        speed_kmh=speed,
        road_condition=road_condition or "dry",
        profile_override=profile_override,
    )


async def _model_prebrief_supplement(prebrief: dict[str, Any]) -> dict[str, Any]:
    try:
        text = await call_model_for_feature(
            [{"role": "user", "content": prebrief_prompt(prebrief)}],
            source="coach",
            task="coach",
            priority=MODEL_PRIORITIES["coach"],
            temperature=0.2,
            max_tokens=220,
            stream=False,
            timeout=18,
        )
        parsed = _parse_model_json_response(text)
        return {
            "status": "ready",
            "text": str(parsed.get("brief") or text).strip(),
            "focus": parsed.get("focus") if isinstance(parsed.get("focus"), list) else [],
            "risk": str(parsed.get("risk") or "").strip(),
            "error": "",
        }
    except Exception as exc:
        return {"status": "error", "text": "", "focus": [], "risk": "", "error": str(exc)}


def _parse_model_json_response(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        return json.loads(candidate)
    except Exception:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except Exception:
                return {}
        return {}


def _prebrief_from_inputs(body: dict[str, Any], frames: list[dict[str, Any]]) -> dict[str, Any]:
    track_id = str(body.get("track_id") or body.get("track") or "").strip() or None
    road_condition_id = str(body.get("road_condition") or "dry").strip() or "dry"
    explicit_style = str(body.get("driver_style") or "auto").strip() or "auto"
    profile_override = body.get("track_profile") if isinstance(body.get("track_profile"), dict) else None
    dist_from_start = body.get("dist_from_start")
    speed_kmh = body.get("speed_kmh")
    profile, track_context = _coach_track_context(
        frames,
        track_id=track_id,
        road_condition=road_condition_id,
        profile_override=profile_override,
        dist_from_start=_optional_float(dist_from_start),
        speed_kmh=_optional_float(speed_kmh),
    )
    common_frames = [to_common_frame(frame) for frame in frames]
    driver_profile = driver_style_profile(common_frames, explicit_style)
    road = road_condition_profile(road_condition_id)
    lookahead_plan = build_lookahead_plan(track_context, driver_profile, road)
    prebrief = build_pre_race_briefing(track_context, driver_profile, road, lookahead_plan)
    return {
        "ok": True,
        "track_profile": {
            "id": profile.get("id"),
            "name": profile.get("name"),
            "length_m": profile.get("length_m"),
            "summary": profile.get("summary"),
        },
        "track_model": track_context,
        "driver_profile": driver_profile,
        "road_condition": road,
        "lookahead_plan": lookahead_plan,
        "pre_race_briefing": prebrief,
    }


@app.get("/api/coach/track-profiles")
async def get_coach_track_profiles():
    return {"profiles": list_track_profiles()}


@app.get("/api/coach/prebrief")
async def get_coach_prebrief(
    track_id: str | None = None,
    driver_style: str = "auto",
    road_condition: str = "dry",
    dist_from_start: float | None = None,
    use_model: bool = False,
):
    if not runtime_manager.is_enabled("coach"):
        return JSONResponse({"ok": False, "error": "coach feature is disabled"}, status_code=409)
    frames = telemetry_store.recent_frames(16.0)
    payload = _prebrief_from_inputs(
        {
            "track_id": track_id,
            "driver_style": driver_style,
            "road_condition": road_condition,
            "dist_from_start": dist_from_start,
        },
        frames,
    )
    if use_model:
        payload["pre_race_briefing"]["model_supplement"] = await _model_prebrief_supplement(payload["pre_race_briefing"])
    return payload


@app.post("/api/coach/prebrief")
async def post_coach_prebrief(body: dict[str, Any]):
    if not runtime_manager.is_enabled("coach"):
        return JSONResponse({"ok": False, "error": "coach feature is disabled"}, status_code=409)
    frames = telemetry_store.recent_frames(float(body.get("history_seconds", 16.0)))
    payload = _prebrief_from_inputs(body, frames)
    if bool(body.get("use_model", False)):
        payload["pre_race_briefing"]["model_supplement"] = await _model_prebrief_supplement(payload["pre_race_briefing"])
    return payload


@app.get("/api/coach/dashboard")
async def get_coach_dashboard(
    window_seconds: float = 6.0,
    history_seconds: float = 16.0,
    track_id: str | None = None,
    driver_style: str = "auto",
    road_condition: str = "dry",
):
    if not runtime_manager.is_enabled("coach"):
        return JSONResponse({"ok": False, "error": "coach feature is disabled"}, status_code=409)
    frames = telemetry_store.recent_frames(max(window_seconds, history_seconds))
    _profile, track_context = _coach_track_context(frames, track_id=track_id, road_condition=road_condition)
    return build_dashboard_payload(
        frames,
        window_seconds=window_seconds,
        history_seconds=history_seconds,
        track_context=track_context,
        driver_style=driver_style,
        road_condition=road_condition,
    )


@app.post("/api/engineer/ask")
async def ask_engineer(body: dict):
    if not runtime_manager.is_enabled("engineer"):
        return JSONResponse({"ok": False, "error": "engineer feature is disabled"}, status_code=409)
    question = str(body.get("question") or "").strip()
    if not question:
        return JSONResponse({"ok": False, "error": "question is required"}, status_code=400)

    supplied_state = body.get("car_state")
    if isinstance(supplied_state, dict) and supplied_state:
        car_state = supplied_state
    else:
        telemetry, _rankings = telemetry_store.latest()
        car_state = telemetry_to_car_state(telemetry) if telemetry else empty_car_state()

    engineer_ctx_mgr.add_user(engineer_ctx_mgr.format_engineer_prompt(car_state, question))
    messages = engineer_ctx_mgr.build_messages()
    request_id = str(uuid.uuid4())
    await broadcast({"type": "ai_start", "source": "engineer", "request_id": request_id})
    try:
        answer = await call_model_for_feature(
            messages,
            source="engineer",
            task="engineer",
            priority=MODEL_PRIORITIES["engineer"],
            temperature=0.4,
            max_tokens=180,
            stream=False,
            # 45s was too tight for this local model with the full
            # telemetry-context prompt, especially while TORCS is also
            # competing for GPU/CPU -- bumped to give it more headroom.
            timeout=90,
            request_id=request_id,
        )
    except Exception as exc:
        await broadcast({"type": "error", "source": "engineer", "message": str(exc), "request_id": request_id})
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    engineer_ctx_mgr.add_assistant(answer)
    await broadcast({"type": "ai_done", "source": "engineer", "content": answer, "request_id": request_id})
    return {
        "ok": True,
        "answer": answer,
        "car_state": car_state,
        "stats": engineer_ctx_mgr.stats(),
    }


@app.get("/api/engineer/history")
async def get_engineer_history():
    return {
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "tokens": message.tokens,
                "pinned": message.pinned,
            }
            for message in engineer_ctx_mgr.history
        ],
        "stats": engineer_ctx_mgr.stats(),
    }


@app.post("/api/engineer/clear")
async def clear_engineer_history():
    engineer_ctx_mgr.clear_history()
    return {"ok": True, "stats": engineer_ctx_mgr.stats()}


@app.get("/api/engineer/voice/available")
async def get_engineer_voice_available():
    """Best-effort check for the dashboard mic button, same probe
    chat_engineer_gui.py relies on before it lets you record. Runs off the
    event loop: it shells out to `pactl` with up to a 3s timeout, which
    would otherwise stall commentary/coach/engineer requests sharing this
    same asyncio loop."""
    available = await asyncio.to_thread(voice_input.mic_available)
    return {"available": available}


@app.post("/api/engineer/voice/start")
async def start_engineer_voice():
    global _engineer_voice_recorder
    if not runtime_manager.is_enabled("engineer"):
        return JSONResponse({"ok": False, "error": "engineer feature is disabled"}, status_code=409)
    if _engineer_voice_recorder is not None:
        return JSONResponse({"ok": False, "error": "a recording is already in progress"}, status_code=409)
    if not await asyncio.to_thread(voice_input.mic_available):
        return JSONResponse(
            {"ok": False, "error": "microphone not available (see docs/voice-input-setup.md)"},
            status_code=503,
        )
    recorder = voice_input.Recorder()
    recorder.start()
    _engineer_voice_recorder = recorder
    return {"ok": True, "recording": True}


@app.post("/api/engineer/voice/stop")
async def stop_engineer_voice():
    """Stop the in-progress recording and transcribe it. Transcription runs
    in a worker thread (faster-whisper is a blocking CPU call) so it never
    stalls the event loop that the other three features' model calls and
    the WebSocket broadcast share."""
    global _engineer_voice_recorder
    recorder = _engineer_voice_recorder
    _engineer_voice_recorder = None
    if recorder is None:
        return JSONResponse({"ok": False, "error": "no recording in progress"}, status_code=409)
    wav_path = await asyncio.to_thread(recorder.stop)
    if not wav_path:
        return {"ok": True, "text": ""}
    text = await asyncio.to_thread(voice_input.transcribe, wav_path)
    return {"ok": True, "text": text}


@app.get("/api/bot/status")
async def get_bot_status():
    return {"status": bot_status_service.snapshot().model_dump(mode="json")}


@app.post("/api/bot/status")
async def update_bot_status(body: dict):
    if not runtime_manager.is_enabled("bot") and bool(body.get("connected")):
        return JSONResponse({"ok": False, "error": "bot feature is disabled"}, status_code=409)
    if "speed" in body and "speed_kmh" not in body:
        body = {**body, "speed_kmh": body["speed"]}
    try:
        update = BotStatusUpdate.model_validate(body)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    bot_status = bot_status_service.update(update).model_dump(mode="json")
    await broadcast({
        "type": "message",
        "source": "bot",
        "level": "error" if bot_status.get("error") else "info",
        "title": "Bot Status",
        "content": str(bot_status.get("error") or bot_status.get("strategy") or "Bot status updated"),
        "payload": {"status": bot_status},
    })
    return {"ok": True, "status": bot_status}


@app.post("/api/bot/strategy")
async def request_bot_strategy(body: dict):
    if not runtime_manager.is_enabled("bot"):
        return JSONResponse({"ok": False, "error": "bot feature is disabled"}, status_code=409)
    try:
        request = BotStrategyRequest.model_validate(body)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    prompt = (
        "You are a TORCS race strategist. Return JSON only with strategy and reason. "
        "strategy must be ATTACK, NORMAL, DEFEND, SAVE_FUEL, or PIT. "
        # Without an explicit cap Granite writes a full paragraph of reasoning,
        # which ran past max_tokens and truncated the JSON mid-string.
        "reason must be a single phrase of at most 8 words.\n"
        + json.dumps(request.model_dump(mode="json"), ensure_ascii=True)
    )
    try:
        text = await call_model_for_feature(
            [{"role": "user", "content": prompt}],
            source="bot",
            task="bot_strategy",
            priority=MODEL_PRIORITIES["bot_strategy"],
            temperature=0.1,
            # 80 was not enough headroom: Granite emits a ```json fence plus a
            # verbose reason, and the object got cut off before its closing
            # brace, so nothing downstream could parse it.
            max_tokens=160,
            # granite-4.1-8b measured at 11.7-12.5s for this prompt on the
            # local LM Studio server, so the previous 10s ceiling timed out
            # on every single call (execution_s=10.010 status=error).
            timeout=30,
        )
        # Granite wraps its answer in a ```json fence, which bare json.loads
        # chokes on; extract_json_object() falls back to the outermost {...}.
        parsed = extract_json_object(text)
        if parsed is None:
            raise ValueError(f"model returned no parsable JSON object: {text[:200]!r}")
        decision = StrategyDecision(
            strategy=Strategy(str(parsed.get("strategy", "NORMAL")).upper()),
            reason=str(parsed.get("reason", "")),
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return {"ok": True, "decision": decision.model_dump(mode="json")}


@app.post("/api/bot/process/start")
async def start_bot_process():
    """Start the ai_bot.py client subprocess from the dashboard.

    This only manages the local `ai_bot.py --bot --granite` client process --
    it does not launch or configure TORCS itself. TORCS must already be
    running in SCR mode (`./BUILD/bin/torcs -ver 2013`, Quick Race ->
    scr_server) or the client will simply fail its own handshake, same as
    running it by hand. Gated behind the `bot` feature switch so the
    behaviour matches every other feature action in this file (409 when
    disabled) instead of silently spawning a process the UI has marked off.
    """
    if not runtime_manager.is_enabled("bot"):
        return JSONResponse({"ok": False, "error": "bot feature is disabled"}, status_code=409)
    proc = process_registry.get("ai_bot")
    if proc is None:
        return JSONResponse({"ok": False, "error": "unknown process: ai_bot"}, status_code=404)
    err = proc.start()
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=500)
    await broadcast({
        "type": "message",
        "source": "bot",
        "level": "info",
        "title": "Bot Process",
        "content": "ai_bot.py started",
        "payload": {"process": proc.status()},
    })
    return {"ok": True, "process": proc.status()}


@app.post("/api/bot/process/stop")
async def stop_bot_process():
    proc = process_registry.get("ai_bot")
    if proc is None:
        return JSONResponse({"ok": False, "error": "unknown process: ai_bot"}, status_code=404)
    # stop() can block up to ~5s waiting for SIGTERM/SIGKILL to land -- push
    # it off the event loop so it can't stall commentary/engineer/coach
    # requests sharing this same asyncio loop while the bot shuts down.
    await asyncio.to_thread(proc.stop)
    await broadcast({
        "type": "message",
        "source": "bot",
        "level": "info",
        "title": "Bot Process",
        "content": "ai_bot.py stopped",
        "payload": {"process": proc.status()},
    })
    return {"ok": True, "process": proc.status()}


@app.get("/api/bot/process/status")
async def get_bot_process_status():
    proc = process_registry.get("ai_bot")
    if proc is None:
        return JSONResponse({"ok": False, "error": "unknown process: ai_bot"}, status_code=404)
    return {"ok": True, "process": proc.status()}


@app.post("/api/telemetry/push")
async def push_telemetry(body: dict):
    """手动 POST 遥测数据（测试用）。"""
    telemetry = body.get("telemetry", {})
    rankings = body.get("rankings", [])
    telemetry_store.push(telemetry, rankings)
    await broadcast({"type": "telemetry_update", "telemetry": telemetry, "rankings": rankings})
    return {"ok": True}


@app.get("/api/events/recent")
async def get_recent_events():
    return {"events": commentary_engine.recent_events}


@app.post("/api/csv/load")
async def load_csv(body: dict):
    """
    从 CSV 文件路径读取数据并触发解说。
    body: { "path": "/path/to/player-1-*.csv", "rankings_path": "..." }
    """
    csv_path = Path(body.get("path", ""))
    rank_path = body.get("rankings_path")

    if not csv_path.exists():
        return JSONResponse({"error": f"文件不存在: {csv_path}"}, status_code=404)

    # 读取最后一行（最新时刻）
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k: _try_float(v) for k, v in row.items()})

    if not rows:
        return JSONResponse({"error": "CSV 为空"}, status_code=400)

    t = rows[-1]

    # 排名文件
    r = []
    if rank_path:
        rp = Path(rank_path)
        if rp.exists():
            with open(rp, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # 取最新 sim_time 的行
                    r.append({k: _try_float(v) for k, v in row.items()})
            # 过滤最新时刻
            if r:
                latest_t = max(float(x.get("sim_time",0)) for x in r)
                r = [x for x in r if abs(float(x.get("sim_time",0)) - latest_t) < 0.01]

    telemetry_store.push(t, r or None)

    asyncio.create_task(generate_commentary(t, r or None))
    return {"ok": True, "rows_loaded": len(rows), "latest_sim_time": t.get("sim_time")}


def _try_float(v: str):
    try:
        return float(v)
    except (ValueError, TypeError):
        return v


@app.get("/api/stats")
async def get_stats():
    stats = ctx_mgr.stats()
    stats["model_scheduler"] = model_broker.status()
    return stats


def _refresh_runtime_status() -> None:
    has_telemetry = telemetry_store.has_telemetry()
    commentary_running = bool(_commentary_task and not _commentary_task.done())
    runtime_manager.update(
        "commentary",
        available=True,
        active=runtime_manager.is_enabled("commentary") and commentary_engine.config.mode != "off",
        healthy=True,
        details={
            "mode": commentary_engine.config.mode,
            "has_telemetry": has_telemetry,
            "active_generation": commentary_running,
            "ws_clients": len(ws_clients),
            "tts_enabled": bool(tts_config.get("enabled")),
        },
    )
    runtime_manager.update(
        "engineer",
        available=True,
        active=False,
        healthy=True,
        details={
            "integration": "api_and_legacy_cli_gui",
            "api": "/api/engineer/ask",
            "model_base_url": api_config.get("base_url", ""),
            "model": api_config.get("model", ""),
            "history_messages": len(engineer_ctx_mgr.history),
        },
    )
    runtime_manager.update(
        "coach",
        available=True,
        active=runtime_manager.is_enabled("coach") and has_telemetry,
        healthy=has_telemetry,
        last_error="" if has_telemetry else "No telemetry frames available yet.",
        details={
            "api": "/api/coach/dashboard",
            "prebrief_api": "/api/coach/prebrief",
            "track_profiles_api": "/api/coach/track-profiles",
            "legacy_api": "/api/feature2/dashboard on the standalone service",
        },
    )
    current_bot_status = bot_status_service.snapshot()
    runtime_manager.update(
        "bot",
        available=True,
        active=runtime_manager.is_enabled("bot") and current_bot_status.active,
        healthy=current_bot_status.health == "healthy",
        last_error=current_bot_status.error,
        details={
            "integration": "status_adapter",
            "api": "/api/bot/status",
            "strategy": current_bot_status.strategy.value,
            "received_at": current_bot_status.received_at,
            "heartbeat_health": current_bot_status.health,
        },
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    log.info(f"WebSocket 已连接，当前客户端数: {len(ws_clients)}")
    try:
        # 发送初始状态
        await ws.send_json(normalize_outbound_message({
            "type": "connected",
            "source": "system",
            "stats": ctx_mgr.stats(),
            "has_telemetry": telemetry_store.has_telemetry(),
        }))
        while True:
            # 保持连接，接收 ping；同时转发外部客户端（如 overlay_broadcast.py，
            # 见 Feature 1 引擎师聊天机器人）发来的展示消息，让所有浮窗都能收到。
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json(normalize_outbound_message({"type": "pong"}))
                continue

            try:
                parsed = json.loads(msg)
            except (TypeError, ValueError):
                continue

            if (
                isinstance(parsed, dict)
                and parsed.get("source", "system") in ALLOWED_SOURCES
                and parsed.get("type") in {
                "ai_start",
                "token",
                "ai_done",
                "error",
                "message",
                }
            ):
                await broadcast(parsed)
    except WebSocketDisconnect:
        ws_clients.discard(ws)
        log.info(f"WebSocket 断开，剩余客户端: {len(ws_clients)}")


# ---------------------------------------------------------------------------
# 启动事件
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    # The middleware lifespan is the sole production owner of UDP 3101.
    telemetry_service.start()
    log.info(f"UDP 监听器启动 0.0.0.0:{config.TELEMETRY_UDP_PORT}")

    # 启动自动解说循环
    global _auto_task
    _auto_task = asyncio.create_task(_auto_commentary_loop())
    log.info(f"服务启动完成 → {config.MIDWARE_BASE_URL}")


@app.on_event("shutdown")
async def shutdown():
    global _auto_task, _commentary_task
    for task in (_auto_task, _commentary_task):
        if task and not task.done():
            task.cancel()
    telemetry_service.stop()
    await model_broker.close()


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TORCS 解说中间件")
    parser.add_argument("--ui", choices=["text", "voice"], default="text",
                        help="界面模式：text=文字解说(index.html)，voice=语音解说(index2.html)")
    args = parser.parse_args()

    UI_FILE = "index2.html" if args.ui == "voice" else "index.html"
    log.info(f"界面模式: {args.ui} → {UI_FILE}")

    uvicorn.run(app, host="0.0.0.0", port=config.MIDWARE_PORT, reload=False)
