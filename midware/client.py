from __future__ import annotations

import json
from typing import Any
from urllib import request

import config


def post_json(path: str, payload: dict[str, Any], *, base_url: str = config.MIDWARE_BASE_URL, timeout: float = 60.0) -> dict[str, Any]:
    req = request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(path: str, *, base_url: str = config.MIDWARE_BASE_URL, timeout: float = 5.0) -> dict[str, Any]:
    req = request.Request(f"{base_url.rstrip('/')}{path}", headers={"Accept": "application/json"})
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def ask_engineer(question: str, car_state: dict[str, Any], *, base_url: str = config.MIDWARE_BASE_URL) -> str:
    result = post_json(
        "/api/engineer/ask",
        {"question": question, "car_state": car_state},
        base_url=base_url,
        timeout=60.0,
    )
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "engineer request failed"))
    return str(result.get("answer") or "")
