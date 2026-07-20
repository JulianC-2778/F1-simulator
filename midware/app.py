from __future__ import annotations

import argparse
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import config
from midware import runtime
from midware.api import ROUTE_GROUPS, build_grouped_routers


def create_app() -> FastAPI:
    """Build a fresh FastAPI application from grouped compatibility routes."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime.startup()
        try:
            yield
        finally:
            await runtime.shutdown()

    app = FastAPI(title="TORCS AI Middleware", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=runtime.STATIC_DIR), name="static")
    routers = build_grouped_routers(runtime.app.routes)
    for name in ("health", "telemetry", "commentary", "engineer", "coach", "bot", "config", "websocket"):
        app.include_router(routers[name])
    app.state.route_groups = ROUTE_GROUPS
    app.state.telemetry_service = runtime.telemetry_service
    app.state.model_broker = runtime.model_broker
    app.state.feature_gate = runtime.runtime_manager
    app.state.bot_status_service = runtime.bot_status_service
    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="TORCS AI middleware")
    parser.add_argument("--ui", choices=["text", "voice"], default="text")
    args = parser.parse_args()
    runtime.UI_FILE = "index2.html" if args.ui == "voice" else "index.html"
    uvicorn.run(app, host="0.0.0.0", port=config.MIDWARE_PORT, reload=False)


if __name__ == "__main__":
    main()
