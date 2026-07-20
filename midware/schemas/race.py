from __future__ import annotations

from pydantic import BaseModel, Field


class CarState(BaseModel):
    speed_kmh: float = 0.0
    rpm: float = 0.0
    gear: int = 0
    fuel_l: float = 0.0
    damage: float = 0.0
    track_position: float = 0.0
    lap_time_s: float = 0.0
    problems: list[str] = Field(default_factory=list)
    tire_wear_percent: float | None = None
