"""Deprecated one-cycle proxy for the unified Coach API."""

from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import config


app = FastAPI(title="TORCS Feature 2 Compatibility Proxy")
UPSTREAM = config.MIDWARE_BASE_URL
STATIC = Path(__file__).resolve().parent / "static" / "feature2.html"


@app.get("/")
@app.get("/feature2")
async def page():
    return HTMLResponse(STATIC.read_text(encoding="utf-8") if STATIC.exists() else "Feature 2")


async def _proxy(path: str):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{UPSTREAM}{path}")
        return JSONResponse(response.json(), status_code=response.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.get("/api/feature2/dashboard")
async def dashboard(window_seconds: float = 6.0, history_seconds: float = 16.0):
    return await _proxy(f"/api/coach/dashboard?window_seconds={window_seconds}&history_seconds={history_seconds}")


@app.get("/api/feature2/health")
async def health():
    return await _proxy("/api/health")


if __name__ == "__main__":
    print("DEPRECATED: use GET /api/coach/dashboard; compatibility proxy removal target: v2.")
    uvicorn.run(app, host="0.0.0.0", port=config.FEATURE2_PORT, reload=False)
