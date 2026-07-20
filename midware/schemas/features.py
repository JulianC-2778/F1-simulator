from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FeatureStatus(BaseModel):
    name: str
    enabled: bool
    available: bool = True
    healthy: bool = True
    active: bool = False
    last_error: str = ""
    last_update: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)
