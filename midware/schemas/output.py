from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


OutputSource = Literal["system", "telemetry", "commentary", "engineer", "coach", "bot"]


class OutputMessageV1(BaseModel):
    version: Literal[1] = 1
    type: str
    source: OutputSource
    request_id: str
    sequence: int = 0
    timestamp: float
    level: str = "info"
    content: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
