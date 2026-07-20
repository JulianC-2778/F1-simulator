from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TelemetryFrame(BaseModel):
    """Canonical, unit-labelled telemetry contract after the TORCS boundary."""

    model_config = ConfigDict(extra="ignore")

    seq: int = 0
    sim_time_s: float = 0.0
    lap: int = 0
    speed_x_kmh: float = 0.0
    speed_y_kmh: float = 0.0
    rpm: float = 0.0
    gear: int = 0
    fuel_l: float = 0.0
    damage: float = 0.0
    track_position: float = 0.0
    current_lap_time_s: float = 0.0
    last_lap_time_s: float = 0.0
    race_position: int = 0
    throttle: float = 0.0
    brake: float = 0.0
    steer: float = 0.0
    distance_from_start_m: float = 0.0
    distance_raced_m: float = 0.0
    track_sensors_m: list[float] = Field(default_factory=list)
    opponent_sensors_m: list[float] = Field(default_factory=list)
