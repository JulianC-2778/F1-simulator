from __future__ import annotations

import time
from typing import Any


class FeatureGate:
    """Own user intent separately from observed runtime health and activity."""

    def __init__(self, feature_specs: list[dict[str, Any]]) -> None:
        self._specs = {str(item["name"]): dict(item) for item in feature_specs}
        self._enabled = set(self._specs)
        self._runtime: dict[str, dict[str, Any]] = {
            name: {
                "available": True,
                "healthy": True,
                "active": False,
                "last_error": "",
                "last_update": 0.0,
                "details": {},
            }
            for name in self._specs
        }

    def is_enabled(self, name: str) -> bool:
        self._require_known(name)
        return name in self._enabled

    def features(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._specs.values()]

    def enabled(self) -> list[str]:
        return sorted(self._enabled)

    def set_enabled(self, names: list[str]) -> None:
        unknown = sorted(set(names) - set(self._specs))
        if unknown:
            raise ValueError(f"Unknown feature(s): {', '.join(unknown)}")
        self._enabled = set(names)
        now = round(time.time(), 3)
        for name, state in self._runtime.items():
            if name not in self._enabled:
                state["active"] = False
            state["last_update"] = now

    def update(
        self,
        name: str,
        *,
        available: bool | None = None,
        healthy: bool | None = None,
        active: bool | None = None,
        last_error: str | None = None,
        details: dict[str, Any] | None = None,
        # Compatibility input from the former runtime manager.
        lifecycle: str | None = None,
        running: bool | None = None,
    ) -> None:
        self._require_known(name)
        state = self._runtime[name]
        if available is not None:
            state["available"] = available
        if healthy is not None:
            state["healthy"] = healthy
        if active is not None:
            state["active"] = active
        elif running is not None:
            state["active"] = running
        if name not in self._enabled:
            state["active"] = False
        if last_error is not None:
            state["last_error"] = last_error
        if details is not None:
            state["details"] = {**state.get("details", {}), **details}
        state["last_update"] = round(time.time(), 3)

    def status(self) -> list[dict[str, Any]]:
        output = []
        for name, runtime in self._runtime.items():
            enabled = name in self._enabled
            active = bool(runtime["active"] and enabled)
            if not enabled:
                lifecycle = "disabled"
            elif active:
                lifecycle = "running"
            elif not runtime["healthy"]:
                lifecycle = "degraded"
            else:
                lifecycle = "stopped"
            output.append(
                {
                    "name": name,
                    "label": self._specs[name].get("label", name),
                    "enabled": enabled,
                    "available": bool(runtime["available"]),
                    "healthy": bool(runtime["healthy"]),
                    "active": active,
                    # Backward-compatible aliases; no process lifecycle is implied.
                    "running": active,
                    "lifecycle": lifecycle,
                    "last_error": runtime["last_error"],
                    "last_update": runtime["last_update"],
                    "details": dict(runtime.get("details") or {}),
                }
            )
        return output

    def combination(self) -> dict[str, Any]:
        enabled = self.enabled()
        return {
            "enabled": enabled,
            "count": len(enabled),
            "supported": len(enabled) <= len(self._specs),
            "available_features": sorted(self._specs),
        }

    def _require_known(self, name: str) -> None:
        if name not in self._specs:
            raise ValueError(f"Unknown feature: {name}")
