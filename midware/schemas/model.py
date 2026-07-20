from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ModelRequest(BaseModel):
    request_id: str
    feature: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    priority: int
    timeout_s: float
    max_tokens: int


class ModelResult(BaseModel):
    request_id: str
    content: str = ""
    status: str = "ok"
    wait_time_s: float = 0.0
    execution_time_s: float = 0.0
    error: str = ""
