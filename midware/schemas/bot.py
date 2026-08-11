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
    # Populated only by the "reasoning" bot-prompt variant (see
    # midware/bot_strategy.py): the factors the model weighed and the option
    # it ruled out, surfaced so the dashboard can show the deliberation
    # rather than just the verdict.  Default-empty, so every other prompt
    # variant and every existing consumer is unaffected.
    considered: list[dict[str, Any]] = Field(default_factory=list)
    rejected: dict[str, Any] = Field(default_factory=dict)
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
