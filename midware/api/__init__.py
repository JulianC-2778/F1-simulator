"""Route grouping helpers used by the application factory."""

from collections.abc import Iterable

from fastapi import APIRouter
from fastapi.routing import APIRoute, APIWebSocketRoute

from midware.api.bot import BOT_PREFIXES
from midware.api.coach import COACH_PREFIXES
from midware.api.commentary import COMMENTARY_PREFIXES
from midware.api.config import CONFIG_PREFIXES
from midware.api.engineer import ENGINEER_PREFIXES
from midware.api.health import HEALTH_PREFIXES
from midware.api.telemetry import TELEMETRY_PREFIXES
from midware.api.websocket import WEBSOCKET_PATHS

ROUTE_GROUPS = {
    "health": HEALTH_PREFIXES,
    "telemetry": TELEMETRY_PREFIXES,
    "commentary": COMMENTARY_PREFIXES,
    "engineer": ENGINEER_PREFIXES,
    "coach": COACH_PREFIXES,
    "bot": BOT_PREFIXES,
    "config": CONFIG_PREFIXES,
    "websocket": WEBSOCKET_PATHS,
}


def build_grouped_routers(routes: Iterable[object]) -> dict[str, APIRouter]:
    """Place extracted compatibility endpoints into real API routers."""
    routers = {name: APIRouter() for name in ROUTE_GROUPS}
    for route in routes:
        if not isinstance(route, (APIRoute, APIWebSocketRoute)):
            continue
        path = route.path
        group = _group_for_path(path)
        if group:
            routers[group].routes.append(route)
    return routers


def _group_for_path(path: str) -> str | None:
    if path == "/ws":
        return "websocket"
    if path == "/" or path in {"/api/health", "/api/stats"}:
        return "health"
    for name in ("commentary", "engineer", "coach", "bot", "config", "telemetry"):
        if any(path.startswith(prefix) for prefix in ROUTE_GROUPS[name]):
            return name
    return None
