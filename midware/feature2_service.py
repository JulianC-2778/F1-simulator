"""Deprecated one-cycle proxy for the unified Coach API."""

from pathlib import Path
import sys
from urllib.parse import urlencode

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config


app = FastAPI(title="TORCS Feature 2 Compatibility Proxy")
UPSTREAM = config.MIDWARE_BASE_URL
STATIC = Path(__file__).resolve().parent / "static" / "feature2.html"


@app.get("/")
@app.get("/feature2")
async def page():
    return HTMLResponse(STATIC.read_text(encoding="utf-8") if STATIC.exists() else "Feature 2")


async def _proxy(path: str, *, method: str = "GET", body: dict | None = None):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "POST":
                response = await client.post(f"{UPSTREAM}{path}", json=body or {})
            else:
                response = await client.get(f"{UPSTREAM}{path}")
        return JSONResponse(response.json(), status_code=response.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.get("/api/feature2/dashboard")
async def dashboard(
    window_seconds: float = 6.0,
    history_seconds: float = 16.0,
    track_id: str | None = None,
    driver_style: str = "auto",
    road_condition: str = "dry",
):
    query = urlencode(
        {
            "window_seconds": window_seconds,
            "history_seconds": history_seconds,
            "track_id": track_id or "",
            "driver_style": driver_style,
            "road_condition": road_condition,
        }
    )
    return await _proxy(f"/api/coach/dashboard?{query}")


@app.get("/api/feature2/track-profiles")
async def track_profiles():
    return await _proxy("/api/coach/track-profiles")


@app.get("/api/feature2/prebrief")
async def get_prebrief(
    track_id: str | None = None,
    driver_style: str = "auto",
    road_condition: str = "dry",
    dist_from_start: float | None = None,
    use_model: bool = False,
):
    params = {
        "driver_style": driver_style,
        "road_condition": road_condition,
        "use_model": str(use_model).lower(),
    }
    if track_id:
        params["track_id"] = track_id
    if dist_from_start is not None:
        params["dist_from_start"] = dist_from_start
    query = urlencode(params)
    return await _proxy(f"/api/coach/prebrief?{query}")


@app.post("/api/feature2/prebrief")
async def post_prebrief(body: dict):
    return await _proxy("/api/coach/prebrief", method="POST", body=body)


@app.get("/api/feature2/health")
async def health():
    return await _proxy("/api/health")


if __name__ == "__main__":
    print("DEPRECATED: use GET /api/coach/dashboard; compatibility proxy removal target: v2.")
    uvicorn.run(app, host="0.0.0.0", port=config.FEATURE2_PORT, reload=False)
