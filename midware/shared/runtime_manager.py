from __future__ import annotations

import time
from typing import Any


VALID_LIFECYCLES = {
    "disabled",
    "starting",
    "running",
    "degraded",
    "error",
    "stopping",
    "stopped",
}


class FeatureRuntimeManager:
    """Track desired feature combinations and observed feature runtime state."""

    def __init__(self, feature_specs: list[dict[str, Any]]) -> None:
        self._specs = {str(spec["name"]): dict(spec) for spec in feature_specs}
        self._desired_enabled = set(self._specs)
        self._states: dict[str, dict[str, Any]] = {}
        for name in self._specs:
            self._states[name] = {
                "name": name,
                "enabled": True,
                "lifecycle": "stopped",
                "running": False,
                "healthy": True,
                "last_error": "",
                "last_update": 0.0,
                "details": {},
            }

    def features(self) -> list[dict[str, Any]]:
        return [dict(spec) for spec in self._specs.values()]

    def enabled(self) -> list[str]:
        return sorted(self._desired_enabled)

    def set_enabled(self, names: list[str]) -> None:
        unknown = sorted(set(names) - set(self._specs))
        if unknown:
            raise ValueError(f"Unknown feature(s): {', '.join(unknown)}")
        self._desired_enabled = set(names)
        now = round(time.time(), 3)
        for name, state in self._states.items():
            is_enabled = name in self._desired_enabled
            state["enabled"] = is_enabled
            if not is_enabled and state["lifecycle"] not in {"stopped", "disabled"}:
                state["lifecycle"] = "disabled"
                state["running"] = False
                state["last_update"] = now

    def update(
        self,
        name: str,
        *,
        lifecycle: str | None = None,
        running: bool | None = None,
        healthy: bool | None = None,
        last_error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if name not in self._states:
            raise ValueError(f"Unknown feature: {name}")
        if lifecycle is not None and lifecycle not in VALID_LIFECYCLES:
            raise ValueError(f"Invalid lifecycle: {lifecycle}")

        state = self._states[name]
        if lifecycle is not None:
            state["lifecycle"] = lifecycle
        if running is not None:
            state["running"] = running
        if healthy is not None:
            state["healthy"] = healthy
        if last_error is not None:
            state["last_error"] = last_error
        if details is not None:
            merged = dict(state.get("details") or {})
            merged.update(details)
            state["details"] = merged
        state["enabled"] = name in self._desired_enabled
        state["last_update"] = round(time.time(), 3)

    def status(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for name, state in self._states.items():
            item = dict(state)
            item["details"] = dict(state.get("details") or {})
            spec = self._specs.get(name, {})
            item["label"] = spec.get("label", name)
            output.append(item)
        return output

    def combination(self) -> dict[str, Any]:
        enabled = self.enabled()
        return {
            "enabled": enabled,
            "count": len(enabled),
            "supported": 1 <= len(enabled) <= len(self._specs),
            "available_features": sorted(self._specs),
        }

