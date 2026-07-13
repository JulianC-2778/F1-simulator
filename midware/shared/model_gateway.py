from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import httpx


TokenCallback = Callable[[str], Awaitable[None]]


class OpenAICompatibleGateway:
    """Async model gateway for OpenAI-compatible chat completion endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 256,
        stream: bool = False,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.stream = stream
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, messages: list[dict[str, Any]], *, stream: bool) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": messages,
            "stream": stream,
        }

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        on_token: TokenCallback | None = None,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        full_text = ""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if self.stream:
                async with client.stream(
                    "POST",
                    url,
                    headers=self._headers(),
                    json=self._payload(messages, stream=True),
                ) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        raise RuntimeError(f"API {response.status_code}: {body.decode(errors='replace')[:300]}")
                    async for line in response.aiter_lines():
                        token = extract_stream_token(line)
                        if not token:
                            continue
                        full_text += token
                        if on_token:
                            await on_token(token)
                return full_text

            response = await client.post(
                url,
                headers=self._headers(),
                json=self._payload(messages, stream=False),
            )
            if response.status_code != 200:
                raise RuntimeError(f"API {response.status_code}: {response.text[:300]}")
            data = response.json()
            return str(data["choices"][0]["message"]["content"])


def extract_stream_token(line: str) -> str:
    if not line.startswith("data:"):
        return ""
    chunk = line[5:].strip()
    if chunk in ("", "[DONE]"):
        return ""
    try:
        data = json.loads(chunk)
        return data["choices"][0].get("delta", {}).get("content", "") or ""
    except Exception:
        return ""
