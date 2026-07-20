from __future__ import annotations

import time
from typing import Any

from midware.schemas.bot import BotStatusSnapshot, BotStatusUpdate


class BotStatusService:
    def __init__(self) -> None:
        self._update = BotStatusUpdate(connected=False)
        self._received_at: float | None = None

    def update(self, update: BotStatusUpdate, *, received_at: float | None = None) -> BotStatusSnapshot:
        self._update = update
        self._received_at = time.time() if received_at is None else received_at
        return self.snapshot(now=self._received_at)

    def snapshot(self, *, now: float | None = None) -> BotStatusSnapshot:
        current_time = time.time() if now is None else now
        age = None if self._received_at is None else max(0.0, current_time - self._received_at)
        if not self._update.connected or age is None or age > 5.0:
            health, active = "disconnected", False
        elif age > 2.0:
            health, active = "degraded", True
        else:
            health, active = "healthy", True
        details: dict[str, Any] = dict(self._update.details)
        details["heartbeat_age_s"] = round(age, 3) if age is not None else None
        return BotStatusSnapshot(
            **self._update.model_dump(exclude={"details"}),
            details=details,
            received_at=self._received_at,
            health=health,
            active=active,
        )
