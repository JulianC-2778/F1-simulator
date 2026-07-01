"""
TORCS 比赛解说中间件 — 主服务

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
import csv
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from commentary_engine import CommentaryConfig, CommentaryEngine
from context_manager import ContextConfig, ContextManager
from telemetry import TelemetryStore, start_udp_listener


def _is_wsl() -> bool:
    if os.getenv("WSL_DISTRO_NAME"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except Exception:
        return False
    return "microsoft" in release or "wsl" in release


def _wsl_windows_host_candidates() -> list[str]:
    candidates: list[str] = []

    def add(host: str | None) -> None:
        if not host:
            return
        host = host.strip()
        if host and host not in candidates:
            candidates.append(host)

    add(os.getenv("TORCS_WINDOWS_HOST"))
    if not _is_wsl():
        return candidates

    try:
        resolv_conf = Path("/etc/resolv.conf")
        if resolv_conf.exists():
            for line in resolv_conf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("nameserver "):
                    add(line.split(None, 1)[1])
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["sh", "-lc", "ip route | awk '/default/ {print $3}' | head -n 1"],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.5,
        )
        add(result.stdout.strip())
    except Exception:
        pass

    return candidates


def _normalize_api_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        return "http://127.0.0.1:1234/v1"
    if normalized.endswith("/v1") or normalized.endswith("/api/v1"):
        return normalized
    return f"{normalized}/v1"


def _default_local_model_base_url() -> str:
    env_base_url = os.getenv("TORCS_AI_BASE_URL")
    if env_base_url:
        return _normalize_api_base_url(env_base_url)

    for host in _wsl_windows_host_candidates():
        return f"http://{host}:1234/v1"
    return "http://127.0.0.1:1234/v1"


def _is_local_api_base_url(base_url: str) -> bool:
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:
        return False

    if host in {"127.0.0.1", "localhost", "0.0.0.0", "::1", "host.docker.internal"}:
        return True
    if host.startswith("192.168.") or host.startswith("10."):
        return True
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2:
            try:
                second = int(parts[1])
            except ValueError:
                second = -1
            if 16 <= second <= 31:
                return True
    return host in {candidate.lower() for candidate in _wsl_windows_host_candidates()}


def _decode_subprocess_output(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16-le", "gbk", "cp936", "latin-1"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _powershell_json_request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    api_key: str = "",
    timeout: int = 30,
) -> dict[str, Any]:
    body_json = json.dumps(body, ensure_ascii=True) if body is not None else ""
    ps_method = method.replace("'", "''")
    ps_url = url.replace("'", "''")
    ps_api_key = api_key.replace("'", "''")
    ps_body = body_json.replace("'", "''")

    script = f"""
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'
$method = '{ps_method}'
$uri = '{ps_url}'
$timeout = {int(timeout)}
$apiKey = '{ps_api_key}'
$body = @'
{ps_body}
'@
$headers = @{{ 'Accept' = 'application/json' }}
if ($apiKey) {{
  $headers['Authorization'] = "Bearer $apiKey"
}}
if ($method -eq 'POST') {{
  $headers['Content-Type'] = 'application/json'
  $resp = Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -Body $body -TimeoutSec $timeout
}} else {{
  $resp = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers -TimeoutSec $timeout
}}
$resp | ConvertTo-Json -Depth 100 -Compress
"""

    temp_path = None
    try:
        temp_dir = os.getenv("TORCS_POWERSHELL_TEMP_DIR", "/mnt/c/Users/Public") if _is_wsl() else None
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8", dir=temp_dir) as temp_file:
            temp_file.write(script)
            temp_path = temp_file.name

        file_arg = temp_path
        if _is_wsl():
            file_arg = subprocess.check_output(["wslpath", "-w", temp_path], text=True, timeout=5).strip()

        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", file_arg],
            capture_output=True,
            timeout=timeout + 5,
            check=False,
        )
        stdout = _decode_subprocess_output(result.stdout)
        stderr = _decode_subprocess_output(result.stderr)
        if result.returncode != 0:
            message = stderr.strip() or stdout.strip() or "unknown PowerShell error"
            raise RuntimeError(message)
        text = stdout.strip()
        if not text:
            raise RuntimeError("PowerShell request returned empty output.")
        return json.loads(text)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
UI_FILE = "index.html"  # 可被启动参数覆盖

# -- AI API 配置（可在 UI 中修改）--
api_config: dict[str, Any] = {
    "provider":  "openai",          # "openai" | "anthropic" | "ollama"
    "base_url":  _default_local_model_base_url(),
    "api_key":   os.getenv("TORCS_AI_API_KEY", ""),
    "model":     "gpt-4o-mini",
    "temperature": 0.8,
    "stream":    True,
}

# -- Context 配置 --
ctx_cfg   = ContextConfig()
ctx_mgr   = ContextManager(ctx_cfg)

# -- 遥测数据缓存（UDP 线程写入，主线程读） --
telemetry_store = TelemetryStore(window_seconds=30.0)

# -- WebSocket 客户端集合 --
ws_clients: set[WebSocket] = set()

# -- 自动解说配置 --
commentary_engine = CommentaryEngine(
    CommentaryConfig(
        mode="interval",
        baseline_interval=10.0,
        event_cooldown=1.0,
        window_seconds=6.0,
        dedupe_seconds=10.0,
        max_words=45,
    )
)
_auto_task: asyncio.Task | None = None
FEATURE2_ASYNC_TIMEOUT_SECONDS = float(os.getenv("TORCS_FEATURE2_ASYNC_TIMEOUT", "18.0"))
FEATURE2_ASYNC_CACHE_LIMIT = int(os.getenv("TORCS_FEATURE2_ASYNC_CACHE_LIMIT", "48"))
_feature2_async_cache: dict[str, dict[str, Any]] = {}
_feature2_async_tasks: dict[str, asyncio.Task[Any]] = {}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="TORCS 比赛解说中间件")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# 广播消息给所有 WebSocket 客户端
# ---------------------------------------------------------------------------

async def broadcast(msg: dict):
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


# ---------------------------------------------------------------------------
# AI 调用
# ---------------------------------------------------------------------------

async def call_ai(messages: list[dict]) -> str:
    """
    调用 AI API 并返回完整回复文本。
    同时通过 WebSocket 流式推送 token。
    """
    provider = api_config["provider"]
    key      = api_config["api_key"]
    base_url = _normalize_api_base_url(str(api_config["base_url"]))
    model    = api_config["model"]
    temp     = api_config["temperature"]
    do_stream = api_config["stream"]

    headers = {"Content-Type": "application/json"}

    # ---- Anthropic ----
    if provider == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
        system_content = next((m["content"] for m in messages if m["role"] == "system"), "")
        filtered = [m for m in messages if m["role"] != "system"]
        payload = {
            "model": model,
            "max_tokens": ctx_cfg.max_response_tokens,
            "temperature": temp,
            "system": system_content,
            "messages": filtered,
            "stream": do_stream,
        }
        url = f"{base_url}/messages"

    # ---- OpenAI-compatible / Ollama ----
    else:
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": model,
            "max_tokens": ctx_cfg.max_response_tokens,
            "temperature": temp,
            "messages": messages,
            "stream": do_stream,
        }
        url = f"{base_url}/chat/completions"

    full_text = ""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if do_stream:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise RuntimeError(f"API {resp.status_code}: {body.decode()[:300]}")
                    async for line in resp.aiter_lines():
                        token = _extract_stream_token(line, provider)
                        if token:
                            full_text += token
                            await broadcast({"type": "token", "text": token})
            else:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code != 200:
                    raise RuntimeError(f"API {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                if provider == "anthropic":
                    full_text = data["content"][0]["text"]
                else:
                    full_text = data["choices"][0]["message"]["content"]
                await broadcast({"type": "token", "text": full_text})
    except httpx.HTTPError:
        if not (_is_wsl() and provider != "anthropic" and _is_local_api_base_url(base_url)):
            raise

        proxy_payload = {
            "model": model,
            "max_tokens": ctx_cfg.max_response_tokens,
            "temperature": temp,
            "messages": messages,
        }
        proxy_data = _powershell_json_request(
            "http://127.0.0.1:1234/v1/chat/completions",
            method="POST",
            body=proxy_payload,
            api_key=key,
            timeout=60,
        )
        full_text = proxy_data["choices"][0]["message"]["content"]
        await broadcast({"type": "token", "text": full_text})

    return full_text


async def call_ai_once(
    messages: list[dict],
    *,
    max_tokens: int = 180,
    temperature: float = 0.2,
    timeout: float = 30.0,
) -> str:
    provider = api_config["provider"]
    key = api_config["api_key"]
    base_url = _normalize_api_base_url(str(api_config["base_url"]))
    model = api_config["model"]

    headers = {"Content-Type": "application/json"}

    if provider == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
        system_content = next((m["content"] for m in messages if m["role"] == "system"), "")
        filtered = [m for m in messages if m["role"] != "system"]
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_content,
            "messages": filtered,
            "stream": False,
        }
        url = f"{base_url}/messages"
    else:
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
            "stream": False,
        }
        url = f"{base_url}/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"API {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            if provider == "anthropic":
                return data["content"][0]["text"]
            return data["choices"][0]["message"]["content"]
    except httpx.HTTPError:
        if not (_is_wsl() and provider != "anthropic" and _is_local_api_base_url(base_url)):
            raise

        proxy_payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        proxy_data = _powershell_json_request(
            "http://127.0.0.1:1234/v1/chat/completions",
            method="POST",
            body=proxy_payload,
            api_key=key,
            timeout=max(1, int(timeout)),
        )
        return proxy_data["choices"][0]["message"]["content"]


def _extract_stream_token(line: str, provider: str) -> str:
    """从 SSE 数据行提取单个 token 文本。"""
    if not line.startswith("data:"):
        return ""
    chunk = line[5:].strip()
    if chunk in ("[DONE]", ""):
        return ""
    try:
        data = json.loads(chunk)
        if provider == "anthropic":
            if data.get("type") == "content_block_delta":
                return data.get("delta", {}).get("text", "")
        else:
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
    provider = str(api_config.get("provider", "openai"))
    base_url = str(api_config.get("base_url", ""))
    api_key = str(api_config.get("api_key", ""))
    if not api_key and provider != "ollama" and not _is_local_api_base_url(base_url):
        raise ValueError("API Key 未设置")

    # 1. 构造 user message
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
        "content": user_content,
        "stats": ctx_mgr.stats(),
    })

    # 4. 构建发送给 AI 的消息列表（已裁剪）
    messages = ctx_mgr.build_messages()

    # 5. 调用 AI
    await broadcast({"type": "ai_start"})
    try:
        reply = await call_ai(messages)
    except Exception as e:
        await broadcast({"type": "error", "message": str(e)})
        raise

    # 6. 把 AI 回复存入历史
    ctx_mgr.add_assistant(reply)

    # 7. 广播完成信号
    await broadcast({
        "type": "ai_done",
        "content": reply,
        "stats": ctx_mgr.stats(),
    })

    return reply


# ---------------------------------------------------------------------------
# 自动解说定时任务
# ---------------------------------------------------------------------------

async def _auto_commentary_loop():
    while True:
        cfg = commentary_engine.config
        if cfg.mode == "off":
            await asyncio.sleep(1)
            continue

        await asyncio.sleep(0.5 if cfg.mode in ("event", "hybrid") else max(1.0, cfg.baseline_interval))

        t, r = telemetry_store.latest()
        if t is None:
            continue

        try:
            frames = telemetry_store.recent_frames(cfg.window_seconds)
            decision = commentary_engine.next_decision(frames, r)
            if decision is None:
                continue

            await broadcast({"type": "event_detected", "event": decision.event, "payload": decision.payload})
            reply = await generate_commentary(
                t,
                r,
                event_payload=decision.payload,
                history_mode="summary",
            )
            if not commentary_engine.should_emit_text(reply, float(decision.payload.get("event_time", 0.0))):
                log.info("重复解说已被去重记录标记")
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


@app.get("/api/config")
async def get_config():
    return {
        "api": {**api_config, "api_key": "***" if api_config["api_key"] else ""},
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
        "network": {
            "running_in_wsl": _is_wsl(),
            "windows_host_candidates": _wsl_windows_host_candidates(),
            "recommended_model_base_url": _default_local_model_base_url(),
            "recommended_browser_url": "http://localhost:8765",
            "recommended_ws_url": "ws://localhost:8765/ws",
        },
    }


@app.post("/api/config/api")
async def update_api_config(body: dict):
    """更新 AI API 配置（POST JSON）。"""
    for k in ("provider", "base_url", "api_key", "model", "temperature", "stream"):
        if k not in body:
            continue
        if k == "base_url":
            raw_value = str(body[k]).strip()
            api_config[k] = _normalize_api_base_url(raw_value) if raw_value else _default_local_model_base_url()
            continue
        api_config[k] = body[k]
    return {"ok": True}


@app.post("/api/config/context")
async def update_context_config(body: dict):
    """更新上下文配置。"""
    global ctx_cfg, ctx_mgr
    for k, v in body.items():
        if hasattr(ctx_cfg, k):
            setattr(ctx_cfg, k, v)
    ctx_mgr.config = ctx_cfg
    return {"ok": True, "stats": ctx_mgr.stats()}


@app.post("/api/config/auto_interval")
async def update_auto_interval(body: dict):
    interval = float(body.get("interval", 0))
    commentary_engine.config.baseline_interval = interval
    commentary_engine.config.mode = "interval" if interval > 0 else "off"
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
    return {"ok": True, "config": await get_commentary_config()}


@app.post("/api/commentary/manual")
async def manual_commentary(body: dict):
    """手动触发一次解说（可附带自定义 prompt）。"""
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


@app.get("/api/telemetry/history")
async def get_telemetry_history(seconds: float | None = None):
    return {"frames": telemetry_store.recent_frames(seconds), "rankings": telemetry_store.recent_rankings(seconds)}


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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\n", " ").replace("\r", " ").split()).strip()


def _truncate_text(value: Any, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = _clean_text(text)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    variance = sum((value - avg) ** 2 for value in values) / len(values)
    return variance ** 0.5


def _safe_min(values: list[float], default: float = 0.0) -> float:
    return min(values) if values else default


def _count_threshold_events(values: list[float], threshold: float) -> int:
    count = 0
    above = False
    for value in values:
        current = value > threshold
        if current and not above:
            count += 1
        above = current
    return count


def _select_recent_frames(frames: list[dict[str, Any]], window_seconds: float) -> list[dict[str, Any]]:
    if not frames:
        return []
    cutoff = frames[-1]["sim_time"] - window_seconds
    return [frame for frame in frames if frame["sim_time"] >= cutoff]


def _summarize_frames(frames: list[dict[str, Any]]) -> dict[str, Any]:
    if not frames:
        return {}

    latest = frames[-1]
    first = frames[0]
    speeds = [frame["speed_x"] for frame in frames]
    throttles = [frame["throttle"] for frame in frames]
    brakes = [frame["brake"] for frame in frames]
    steering = [frame["steer"] for frame in frames]
    track_positions = [frame["track_pos"] for frame in frames]
    angles = [frame["angle"] for frame in frames]
    rpms = [frame["rpm"] for frame in frames]
    front_track = [frame["track"][9] for frame in frames if len(frame["track"]) > 9]
    nearest_opponents = [
        min((distance for distance in frame["opponents"] if distance >= 0), default=200.0)
        for frame in frames
    ]

    return {
        "frame_count": len(frames),
        "duration": max(0.0, latest["sim_time"] - first["sim_time"]),
        "avg_speed": _mean(speeds),
        "max_speed": max(speeds) if speeds else 0.0,
        "min_speed": min(speeds) if speeds else 0.0,
        "avg_throttle": _mean(throttles),
        "avg_brake": _mean(brakes),
        "avg_rpm": _mean(rpms),
        "peak_rpm": max(rpms) if rpms else 0.0,
        "brake_events": _count_threshold_events(brakes, 0.15),
        "throttle_lifts": _count_threshold_events([1.0 - value for value in throttles], 0.7),
        "off_track_moments": sum(1 for value in track_positions if abs(value) > 1.0),
        "edge_pressure_moments": sum(1 for value in track_positions if abs(value) > 0.8),
        "avg_track_pos": _mean(track_positions),
        "track_pos_stddev": _stddev(track_positions),
        "steering_stddev": _stddev(steering),
        "angle_stddev": _stddev(angles),
        "front_track_clearance_avg": _mean(front_track),
        "front_track_clearance_now": latest["track"][9] if len(latest["track"]) > 9 else -1.0,
        "nearest_opponent_now": nearest_opponents[-1] if nearest_opponents else 200.0,
        "nearest_opponent_window": min(nearest_opponents) if nearest_opponents else 200.0,
        "damage_delta": latest["damage"] - first["damage"],
        "speed_delta": latest["speed_x"] - first["speed_x"],
    }


def _latest_state_payload(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": frame["seq"],
        "sim_time": round(frame["sim_time"], 3),
        "lap": frame["lap"],
        "cur_lap_time": round(frame["cur_lap_time"], 3),
        "last_lap_time": round(frame["last_lap_time"], 3),
        "speed_x": round(frame["speed_x"], 3),
        "speed_y": round(frame["speed_y"], 3),
        "speed_z": round(frame["speed_z"], 3),
        "gear": frame["gear"],
        "rpm": round(frame["rpm"], 1),
        "throttle": round(frame["throttle"], 3),
        "brake": round(frame["brake"], 3),
        "steer": round(frame["steer"], 3),
        "track_pos": round(frame["track_pos"], 3),
        "angle": round(frame["angle"], 3),
        "damage": round(frame["damage"], 3),
        "fuel": round(frame["fuel"], 3),
        "race_pos": frame["race_pos"],
        "dist_from_start": round(frame["dist_from_start"], 3),
    }


def _compact_track_profile(track: list[float]) -> dict[str, float]:
    if len(track) < 19:
        return {
            "left_opening": -1.0,
            "center_opening": -1.0,
            "right_opening": -1.0,
            "tightest_opening": -1.0,
        }
    left = track[0:6]
    center = track[7:12]
    right = track[13:19]
    return {
        "left_opening": round(_mean(left), 3),
        "center_opening": round(_mean(center), 3),
        "right_opening": round(_mean(right), 3),
        "tightest_opening": round(_safe_min(track, -1.0), 3),
    }


def _compact_opponent_profile(opponents: list[float]) -> dict[str, float]:
    if len(opponents) < 36:
        return {
            "front_gap": 200.0,
            "left_gap": 200.0,
            "right_gap": 200.0,
            "rear_gap": 200.0,
            "nearest_gap": 200.0,
        }
    front = opponents[16:21]
    left = opponents[21:28]
    right = opponents[9:16]
    rear = opponents[0:4] + opponents[32:36]
    nearest = [distance for distance in opponents if distance >= 0]
    return {
        "front_gap": round(_safe_min(front, 200.0), 3),
        "left_gap": round(_safe_min(left, 200.0), 3),
        "right_gap": round(_safe_min(right, 200.0), 3),
        "rear_gap": round(_safe_min(rear, 200.0), 3),
        "nearest_gap": round(_safe_min(nearest, 200.0), 3),
    }


def _compact_live_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "duration": round(summary.get("duration", 0.0), 3),
        "avg_speed": round(summary.get("avg_speed", 0.0), 3),
        "max_speed": round(summary.get("max_speed", 0.0), 3),
        "avg_throttle": round(summary.get("avg_throttle", 0.0), 3),
        "avg_brake": round(summary.get("avg_brake", 0.0), 3),
        "brake_events": int(summary.get("brake_events", 0)),
        "off_track_moments": int(summary.get("off_track_moments", 0)),
        "edge_pressure_moments": int(summary.get("edge_pressure_moments", 0)),
        "track_pos_stddev": round(summary.get("track_pos_stddev", 0.0), 3),
        "steering_stddev": round(summary.get("steering_stddev", 0.0), 3),
        "nearest_opponent_now": round(summary.get("nearest_opponent_now", 200.0), 3),
        "damage_delta": round(summary.get("damage_delta", 0.0), 3),
    }


def _build_rule_feedback(frames: list[dict[str, Any]]) -> dict[str, Any]:
    latest = frames[-1]
    summary = _summarize_frames(frames)
    track_profile = _compact_track_profile(latest["track"])
    opponent_profile = _compact_opponent_profile(latest["opponents"])

    pit_advice = "No pit stop needed yet."
    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        pit_advice = "Pit now. Fuel or damage is already in the danger zone."
    elif latest["fuel"] < 10.0 or latest["damage"] > 25.0:
        pit_advice = "Pit soon. Fuel or damage is trending risky."

    if opponent_profile["front_gap"] < 8.0 and latest["speed_x"] > 60.0:
        return {
            "state_id": "collision_risk",
            "headline": "Traffic Alert",
            "focus_area": "traffic",
            "priority": "high",
            "analysis": f"Front gap is only {opponent_profile['front_gap']:.1f} m, so the overtake or braking window is tight.",
            "action": "Defend the inside and brake slightly earlier to avoid contact with the car ahead.",
            "pit_advice": pit_advice,
            "confidence": 0.95,
        }

    if abs(latest["track_pos"]) > 1.0:
        side = "left" if latest["track_pos"] < 0 else "right"
        return {
            "state_id": "off_track_recovery",
            "headline": "Off Track Risk",
            "focus_area": "cornering",
            "priority": "high",
            "analysis": f"The car is already beyond the {side} track edge with track position {latest['track_pos']:.2f}.",
            "action": "Straighten the steering, ease off the throttle, and rejoin the track smoothly before pushing again.",
            "pit_advice": pit_advice,
            "confidence": 0.98,
        }

    if latest["fuel"] < 6.0 or latest["damage"] > 40.0:
        return {
            "state_id": "pit_now",
            "headline": "Pit Window Open",
            "focus_area": "pit_strategy",
            "priority": "high",
            "analysis": "Fuel or damage has crossed the local safety threshold.",
            "action": "Commit to a pit stop at the next safe opportunity.",
            "pit_advice": pit_advice,
            "confidence": 0.97,
        }

    if track_profile["center_opening"] < 25.0 and latest["brake"] < 0.08 and latest["speed_x"] > 90.0:
        return {
            "state_id": "late_braking",
            "headline": "Braking Point",
            "focus_area": "braking",
            "priority": "medium",
            "analysis": "The road ahead is tightening, but brake pressure is still very low for the current speed.",
            "action": "Brake earlier for the next corner and finish most of the braking before turn-in.",
            "pit_advice": pit_advice,
            "confidence": 0.82,
        }

    if summary.get("steering_stddev", 0.0) > 0.35 and abs(latest["track_pos"]) > 0.6:
        return {
            "state_id": "unstable_line",
            "headline": "Line Stability",
            "focus_area": "cornering",
            "priority": "medium",
            "analysis": "Steering corrections are high while the car is already close to the edge of the track.",
            "action": "Use one smoother steering input and let the car breathe back toward the center on exit.",
            "pit_advice": pit_advice,
            "confidence": 0.80,
        }

    if summary.get("avg_throttle", 0.0) < 0.35 and track_profile["center_opening"] > 60.0 and latest["speed_x"] > 70.0:
        return {
            "state_id": "throttle_hesitation",
            "headline": "Throttle Timing",
            "focus_area": "throttle",
            "priority": "medium",
            "analysis": "There is open road ahead, but throttle application over the last window has stayed conservative.",
            "action": "Start squeezing on the throttle earlier once the steering wheel begins to unwind.",
            "pit_advice": pit_advice,
            "confidence": 0.76,
        }

    return {
        "state_id": "stable_rhythm",
        "headline": "Rhythm Check",
        "focus_area": "cornering",
        "priority": "low",
        "analysis": "The current window looks stable, with no urgent danger signal from the local rules.",
        "action": "Keep building rhythm and focus on a clean entry-to-exit line.",
        "pit_advice": pit_advice,
        "confidence": 0.62,
    }


def _midware_frame_to_common(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": _safe_int(frame.get("seq")),
        "sim_time": _safe_float(frame.get("sim_time")),
        "player": _safe_int(frame.get("player")),
        "lap": _safe_int(frame.get("lap")),
        "x": _safe_float(frame.get("x")),
        "y": _safe_float(frame.get("y")),
        "yaw": _safe_float(frame.get("yaw")),
        "accel_x": _safe_float(frame.get("accel_x")),
        "accel_y": _safe_float(frame.get("accel_y")),
        "steer": _safe_float(frame.get("steer")),
        "throttle": _safe_float(frame.get("throttle")),
        "brake": _safe_float(frame.get("brake")),
        "clutch": _safe_float(frame.get("clutch")),
        "angle": _safe_float(frame.get("angle")),
        "cur_lap_time": _safe_float(frame.get("curLapTime")),
        "damage": _safe_float(frame.get("damage")),
        "dist_from_start": _safe_float(frame.get("distFromStart")),
        "dist_raced": _safe_float(frame.get("distRaced")),
        "fuel": _safe_float(frame.get("fuel")),
        "gear": _safe_int(frame.get("gear")),
        "last_lap_time": _safe_float(frame.get("lastLapTime")),
        "race_pos": _safe_int(frame.get("racePos")),
        "rpm": _safe_float(frame.get("rpm")),
        "speed_x": _safe_float(frame.get("speedX")),
        "speed_y": _safe_float(frame.get("speedY")),
        "speed_z": _safe_float(frame.get("speedZ")),
        "track_pos": _safe_float(frame.get("trackPos")),
        "z": _safe_float(frame.get("z")),
        "opponents": [_safe_float(frame.get(f"opponent_{i}"), 200.0) for i in range(36)],
        "track": [_safe_float(frame.get(f"track_{i}"), -1.0) for i in range(19)],
        "wheel_spin_vel": [_safe_float(frame.get(f"wheelSpinVel_{i}")) for i in range(4)],
        "focus": [_safe_float(frame.get(f"focus_{i}"), -1.0) for i in range(5)],
    }


def _series_points(frames: list[dict[str, Any]], key: str) -> list[dict[str, float]]:
    return [
        {
            "sim_time": round(_safe_float(frame.get("sim_time")), 3),
            "value": round(_safe_float(frame.get(key)), 3),
        }
        for frame in frames
    ]


def _feature2_overlay_key(rule_feedback: dict[str, Any]) -> str:
    return "|".join(
        [
            _clean_text(rule_feedback.get("state_id") or "stable_rhythm"),
            _clean_text(rule_feedback.get("focus_area")),
            _clean_text(rule_feedback.get("priority")),
            _clean_text(rule_feedback.get("action")),
            _clean_text(rule_feedback.get("pit_advice")),
        ]
    )


def _feature2_overlay_payload(
    latest: dict[str, Any],
    summary: dict[str, Any],
    track_profile: dict[str, Any],
    opponent_profile: dict[str, Any],
    rule_feedback: dict[str, Any],
) -> dict[str, Any]:
    return {
        "state_id": rule_feedback.get("state_id", "stable_rhythm"),
        "focus_area": rule_feedback.get("focus_area", "cornering"),
        "priority": rule_feedback.get("priority", "medium"),
        "headline": rule_feedback.get("headline", "Guidance"),
        "action": rule_feedback.get("action", ""),
        "pit_advice": rule_feedback.get("pit_advice", "No pit stop needed yet."),
        "rule_reason": rule_feedback.get("analysis", ""),
        "latest_state": {
            "lap": latest["lap"],
            "speed_x": round(latest["speed_x"], 3),
            "gear": latest["gear"],
            "throttle": round(latest["throttle"], 3),
            "brake": round(latest["brake"], 3),
            "track_pos": round(latest["track_pos"], 3),
            "damage": round(latest["damage"], 3),
            "fuel": round(latest["fuel"], 3),
        },
        "window_summary": {
            "avg_speed": round(summary.get("avg_speed", 0.0), 3),
            "avg_throttle": round(summary.get("avg_throttle", 0.0), 3),
            "avg_brake": round(summary.get("avg_brake", 0.0), 3),
            "brake_events": int(summary.get("brake_events", 0)),
            "track_pos_stddev": round(summary.get("track_pos_stddev", 0.0), 3),
            "steering_stddev": round(summary.get("steering_stddev", 0.0), 3),
            "damage_delta": round(summary.get("damage_delta", 0.0), 3),
        },
        "track_profile": track_profile,
        "opponent_profile": opponent_profile,
    }


def _feature2_overlay_prompt(payload: dict[str, Any]) -> str:
    return f"""You are supplementing a rule-based TORCS telemetry dashboard.
The rule guidance is already fixed. Do not replace the action, headline, focus area, or pit advice.
Your only job is to add a short, useful explanation for the dashboard.

Rules:
1. Output one valid JSON object only.
2. Use English only.
3. Keep the response concise and telemetry-grounded.
4. "analysis" should explain why the current rule guidance makes sense.
5. "coach_note" should be one short supporting sentence for the driver.

Return this schema:
{{
  "analysis": "1-2 short sentences",
  "coach_note": "one short supporting sentence"
}}

Payload:
{json.dumps(payload, ensure_ascii=True)}"""


def _feature2_pending_overlay() -> dict[str, Any]:
    return {
        "status": "pending",
        "source": "model_overlay",
        "analysis": "",
        "coach_note": "",
        "updated_at": None,
        "error": "",
    }


def _trim_feature2_overlay_cache() -> None:
    while len(_feature2_async_cache) > FEATURE2_ASYNC_CACHE_LIMIT:
        oldest_key = next(iter(_feature2_async_cache))
        if oldest_key in _feature2_async_tasks:
            break
        _feature2_async_cache.pop(oldest_key, None)


async def _generate_feature2_overlay(cache_key: str, payload: dict[str, Any]) -> None:
    try:
        text = await call_ai_once(
            [
                {
                    "role": "system",
                    "content": "You are a concise racing engineer assistant. Return stable JSON only.",
                },
                {
                    "role": "user",
                    "content": _feature2_overlay_prompt(payload),
                },
            ],
            max_tokens=160,
            temperature=0.15,
            timeout=FEATURE2_ASYNC_TIMEOUT_SECONDS,
        )
        parsed = _extract_json_object(text) or {}
        analysis = _truncate_text(parsed.get("analysis") or text, 220)
        coach_note = _truncate_text(parsed.get("coach_note"), 140)
        _feature2_async_cache[cache_key] = {
            "status": "ready",
            "source": "model_overlay",
            "analysis": analysis,
            "coach_note": coach_note,
            "updated_at": round(asyncio.get_running_loop().time(), 3),
            "error": "",
        }
    except Exception as exc:
        _feature2_async_cache[cache_key] = {
            "status": "error",
            "source": "model_overlay",
            "analysis": "",
            "coach_note": "",
            "updated_at": round(asyncio.get_running_loop().time(), 3),
            "error": _truncate_text(exc, 180),
        }
    finally:
        _feature2_async_tasks.pop(cache_key, None)
        _trim_feature2_overlay_cache()


def _ensure_feature2_overlay(
    latest: dict[str, Any],
    summary: dict[str, Any],
    track_profile: dict[str, Any],
    opponent_profile: dict[str, Any],
    rule_feedback: dict[str, Any],
) -> dict[str, Any]:
    cache_key = _feature2_overlay_key(rule_feedback)
    overlay = _feature2_async_cache.get(cache_key)
    if overlay is None:
        overlay = _feature2_pending_overlay()
        _feature2_async_cache[cache_key] = overlay

    if cache_key not in _feature2_async_tasks and overlay.get("status") not in {"ready", "error"}:
        overlay_payload = _feature2_overlay_payload(latest, summary, track_profile, opponent_profile, rule_feedback)
        _feature2_async_tasks[cache_key] = asyncio.create_task(_generate_feature2_overlay(cache_key, overlay_payload))

    return dict(overlay, cache_key=cache_key)


def _build_feature2_dashboard(window_seconds: float = 6.0, history_seconds: float = 16.0) -> dict[str, Any]:
    telemetry, _rankings = telemetry_store.latest()
    if telemetry is None:
        return {
            "status": {
                "has_telemetry": False,
                "window_seconds": window_seconds,
                "history_seconds": history_seconds,
                "frame_count": 0,
            },
            "latest_state": None,
            "window_summary": None,
            "track_profile": None,
            "opponent_profile": None,
            "guidance": None,
            "signals": [],
            "history": {
                "speed_x": [],
                "throttle": [],
                "brake": [],
                "track_pos": [],
                "rpm": [],
            },
        }

    lookback_seconds = max(window_seconds, history_seconds)
    raw_frames = telemetry_store.recent_frames(lookback_seconds)
    common_frames = [_midware_frame_to_common(frame) for frame in raw_frames]
    if not common_frames:
        return {
            "status": {
                "has_telemetry": False,
                "window_seconds": window_seconds,
                "history_seconds": history_seconds,
                "frame_count": 0,
            },
            "latest_state": None,
            "window_summary": None,
            "track_profile": None,
            "opponent_profile": None,
            "guidance": None,
            "signals": [],
            "history": {
                "speed_x": [],
                "throttle": [],
                "brake": [],
                "track_pos": [],
                "rpm": [],
            },
        }

    live_frames = _select_recent_frames(common_frames, window_seconds) or common_frames
    history_frames = _select_recent_frames(common_frames, history_seconds) or common_frames
    latest = live_frames[-1]
    summary = _summarize_frames(live_frames)
    rule_feedback = _build_rule_feedback(live_frames)
    track_profile = _compact_track_profile(latest["track"])
    opponent_profile = _compact_opponent_profile(latest["opponents"])
    async_overlay = _ensure_feature2_overlay(latest, summary, track_profile, opponent_profile, rule_feedback)

    track_pos = latest["track_pos"]
    signals = [
        {
            "label": "Track Limit",
            "value": round(track_pos, 3),
            "display": f"{track_pos:+.2f}",
            "tone": "danger" if abs(track_pos) > 1.0 else "warn" if abs(track_pos) > 0.8 else "good",
        },
        {
            "label": "Front Gap",
            "value": opponent_profile["front_gap"],
            "display": f"{opponent_profile['front_gap']:.1f} m",
            "tone": "danger" if opponent_profile["front_gap"] < 8.0 else "warn" if opponent_profile["front_gap"] < 15.0 else "good",
        },
        {
            "label": "Fuel Reserve",
            "value": latest["fuel"],
            "display": f"{latest['fuel']:.1f} L",
            "tone": "danger" if latest["fuel"] < 6.0 else "warn" if latest["fuel"] < 10.0 else "good",
        },
        {
            "label": "Damage Load",
            "value": latest["damage"],
            "display": f"{latest['damage']:.1f}",
            "tone": "danger" if latest["damage"] > 40.0 else "warn" if latest["damage"] > 25.0 else "good",
        },
    ]

    guidance = {
        "analysis_type": "live_window",
        "source": "rule_engine",
        "sim_time": round(latest["sim_time"], 3),
        "state_id": rule_feedback.get("state_id", "stable_rhythm"),
        "headline": rule_feedback["headline"],
        "focus_area": rule_feedback["focus_area"],
        "priority": rule_feedback["priority"],
        "analysis": rule_feedback["analysis"],
        "action": rule_feedback["action"],
        "pit_advice": rule_feedback["pit_advice"],
        "confidence": round(_safe_float(rule_feedback.get("confidence"), 0.0), 2),
        "async_overlay": async_overlay,
    }

    return {
        "status": {
            "has_telemetry": True,
            "window_seconds": window_seconds,
            "history_seconds": history_seconds,
            "frame_count": len(history_frames),
            "latest_sim_time": round(latest["sim_time"], 3),
        },
        "latest_state": _latest_state_payload(latest),
        "window_summary": _compact_live_summary(summary),
        "track_profile": track_profile,
        "opponent_profile": opponent_profile,
        "guidance": guidance,
        "signals": signals,
        "history": {
            "speed_x": _series_points(history_frames, "speed_x"),
            "throttle": _series_points(history_frames, "throttle"),
            "brake": _series_points(history_frames, "brake"),
            "track_pos": _series_points(history_frames, "track_pos"),
            "rpm": _series_points(history_frames, "rpm"),
        },
    }


@app.get("/feature2", response_class=HTMLResponse)
async def feature2_page():
    html_path = STATIC_DIR / "feature2.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>feature2.html not found</h1>", status_code=404)


@app.get("/api/feature2/dashboard")
async def get_feature2_dashboard(window_seconds: float = 6.0, history_seconds: float = 16.0):
    return _build_feature2_dashboard(window_seconds=window_seconds, history_seconds=history_seconds)


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
    return ctx_mgr.stats()


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
        await ws.send_json({
            "type": "connected",
            "stats": ctx_mgr.stats(),
            "has_telemetry": telemetry_store.has_telemetry(),
        })
        while True:
            # 保持连接，接收 ping
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        ws_clients.discard(ws)
        log.info(f"WebSocket 断开，剩余客户端: {len(ws_clients)}")


# ---------------------------------------------------------------------------
# 启动事件
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    # 启动 UDP 监听线程
    start_udp_listener(
        telemetry_store,
        port=3101,
        on_error=lambda exc: log.error(f"UDP 监听器错误: {exc}"),
    )
    log.info("UDP 监听器启动 0.0.0.0:3101")

    # 启动自动解说循环
    global _auto_task
    _auto_task = asyncio.create_task(_auto_commentary_loop())
    log.info("服务启动完成 → http://localhost:8765")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TORCS 解说中间件")
    parser.add_argument("--ui", choices=["text", "voice"], default="text",
                        help="界面模式：text=文字解说(index.html)，voice=语音解说(index2.html)")
    args = parser.parse_args()

    UI_FILE = "index2.html" if args.ui == "voice" else "index.html"
    log.info(f"界面模式: {args.ui} → {UI_FILE}")

    uvicorn.run(app, host="0.0.0.0", port=8765, reload=False)
