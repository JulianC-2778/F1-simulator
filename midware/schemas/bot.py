from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Strategy(str, Enum):
    ATTACK = "ATTACK"
    NORMAL = "NORMAL"
    DEFEND = "DEFEND"
    SAVE_FUEL = "SAVE_FUEL"
    PIT = "PIT"


class BotStrategyRequest(BaseModel):
    bot_id: str = "default"
    current_strategy: Strategy = Strategy.NORMAL
    sensor_state: dict[str, Any] = Field(default_factory=dict)


class StrategyDecision(BaseModel):
    strategy: Strategy = Strategy.NORMAL
    reason: str = ""
    request_id: str = ""
    fallback: bool = False


class BotStatusUpdate(BaseModel):
    connected: bool
    strategy: Strategy = Strategy.NORMAL
    speed_kmh: float = 0.0
    gear: int = 0
    last_control: dict[str, Any] = Field(default_factory=dict)
    fallback: bool = False
    error: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class BotStatusSnapshot(BotStatusUpdate):
    received_at: float | None = None
    health: str = "disconnected"
    active: bool = False
