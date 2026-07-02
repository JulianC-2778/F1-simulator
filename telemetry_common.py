#!/usr/bin/env python3
"""
Shared helpers for TORCS telemetry-driven AI features.
"""

from __future__ import annotations

import atexit
import base64
import json
import math
import os
import platform
import socket
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any
from urllib import request

from openai import OpenAI


DEFAULT_MODEL_BASE_URL = os.getenv("TORCS_AI_BASE_URL", "http://127.0.0.1:1234/v1")
DEFAULT_MODEL_NAME = os.getenv("TORCS_AI_MODEL", "").strip()
DEFAULT_API_KEY = os.getenv("TORCS_AI_API_KEY", "not-needed")
DEFAULT_MODEL_HINT = os.getenv("TORCS_AI_MODEL_HINT", "granite").strip().lower()
DEFAULT_WSL_TRANSPORTS = tuple(
    part.strip().lower()
    for part in os.getenv("TORCS_AI_WSL_TRANSPORTS", "powershell,bridge").split(",")
    if part.strip()
)


@dataclass
class ModelConnection:
    client: OpenAI | None
    base_url: str
    model_name: str
    visible_models: list[str]
    transport: str
    bridge_session: Any = None


@dataclass
class WorkerResult:
    task: dict[str, Any]
    output: Any = None
    error: str | None = None


class WindowsRelaySession:
    def __init__(self, api_key: str = DEFAULT_API_KEY) -> None:
        self.api_key = api_key
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._script_path: str | None = None
        atexit.register(self.close)

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            script_path = self._script_path
            self._script_path = None

        if process is not None and process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        if script_path and os.path.exists(script_path):
            try:
                os.remove(script_path)
            except OSError:
                pass

    def request_text(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: int = 20,
    ) -> str:
        attempts = 1 if method.upper() == "POST" else 2
        for index in range(attempts):
            try:
                return self._request_text_once(url, method=method, body=body, timeout=timeout)
            except Exception:
                if index + 1 >= attempts:
                    raise
                self.close()
        raise RuntimeError("Windows relay request failed.")

    def request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: int = 20,
    ) -> dict[str, Any]:
        return json.loads(self.request_text(url, method=method, body=body, timeout=timeout))

    def _request_text_once(
        self,
        url: str,
        *,
        method: str,
        body: dict[str, Any] | None,
        timeout: int,
    ) -> str:
        with self._lock:
            self._ensure_started()
            if self._process is None or self._process.stdin is None or self._process.stdout is None:
                raise RuntimeError("Windows relay process is not available.")

            payload = {
                "url": url,
                "method": method.upper(),
                "timeout": max(1, int(timeout)),
                "api_key": self.api_key,
                "body": body,
            }
            request_text = json.dumps(payload, ensure_ascii=True)
            encoded = base64.b64encode(request_text.encode("utf-8")).decode("ascii")
            self._process.stdin.write(f"{encoded}\n")
            self._process.stdin.flush()

            response_line = self._process.stdout.readline()
            if not response_line:
                raise RuntimeError("Windows relay returned no response line.")

        response_json = base64.b64decode(response_line.strip()).decode("utf-8")
        response = json.loads(response_json)
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "Windows relay request failed.")))
        content = str(response.get("content", "")).strip()
        if not content:
            raise RuntimeError("Windows relay returned empty content.")
        return content

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return

        script = r"""
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) {
    break
  }
  if ([string]::IsNullOrWhiteSpace($line)) {
    continue
  }
  try {
    $requestJson = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($line))
    $request = $requestJson | ConvertFrom-Json
    $method = [string]$request.method
    $uri = [string]$request.url
    $timeout = [int]$request.timeout
    $apiKey = [string]$request.api_key
    $headers = @{ 'Accept' = 'application/json' }
    if ($apiKey) {
      $headers['Authorization'] = "Bearer $apiKey"
    }
    if ($method -eq 'POST') {
      $headers['Content-Type'] = 'application/json'
      $bodyJson = ''
      if ($null -ne $request.body) {
        $bodyJson = $request.body | ConvertTo-Json -Depth 100 -Compress
      }
      $response = Invoke-WebRequest -Method Post -Uri $uri -Headers $headers -Body $bodyJson -TimeoutSec $timeout
      $content = $response.Content
    } else {
      $response = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers -TimeoutSec $timeout
      $content = $response | ConvertTo-Json -Depth 100 -Compress
    }
    $result = @{ ok = $true; content = $content } | ConvertTo-Json -Depth 20 -Compress
  } catch {
    $result = @{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Depth 20 -Compress
  }
  $encoded = [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($result))
  [Console]::Out.WriteLine($encoded)
  [Console]::Out.Flush()
}
"""

        temp_dir = None
        if is_wsl():
            temp_dir = os.getenv("TORCS_POWERSHELL_TEMP_DIR", "/mnt/c/Users/Public")
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8", dir=temp_dir) as temp_file:
            temp_file.write(script)
            script_path = temp_file.name

        file_arg = script_path
        if is_wsl():
            file_arg = subprocess.check_output(["wslpath", "-w", script_path], text=True, timeout=5).strip()

        process = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", file_arg],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._process = process
        self._script_path = script_path


class LatestTaskRunner:
    def __init__(self, handler: Any, name: str) -> None:
        self.handler = handler
        self.name = name
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._results: Queue[WorkerResult] = Queue()
        self._pending_task: dict[str, Any] | None = None
        self._pending_priority = -10**9
        self._busy = False
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self._thread.start()

    def submit(self, task: dict[str, Any], priority: int = 0) -> bool:
        with self._lock:
            if self._pending_task is not None and priority < self._pending_priority:
                return False
            self._pending_task = task
            self._pending_priority = priority
            self._wake_event.set()
            return True

    def is_busy(self) -> bool:
        with self._lock:
            return self._busy or self._pending_task is not None

    def pop_completed(self) -> WorkerResult | None:
        try:
            return self._results.get_nowait()
        except Empty:
            return None

    def _take_pending(self) -> tuple[dict[str, Any] | None, int]:
        with self._lock:
            task = self._pending_task
            priority = self._pending_priority
            self._pending_task = None
            self._pending_priority = -10**9
            if task is not None:
                self._busy = True
            else:
                self._busy = False
            return task, priority

    def _finish(self) -> None:
        with self._lock:
            self._busy = False
            if self._pending_task is not None:
                self._wake_event.set()

    def _run(self) -> None:
        while True:
            self._wake_event.wait()
            self._wake_event.clear()
            task, _priority = self._take_pending()
            if task is None:
                continue
            try:
                output = self.handler(task)
                self._results.put(WorkerResult(task=task, output=output))
            except Exception as exc:
                self._results.put(WorkerResult(task=task, error=str(exc)))
            finally:
                self._finish()


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value: str, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def safe_min(values: list[float], default: float = 0.0) -> float:
    return min(values) if values else default


def safe_max(values: list[float], default: float = 0.0) -> float:
    return max(values) if values else default


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def build_openai_client(
    base_url: str = DEFAULT_MODEL_BASE_URL,
    api_key: str = DEFAULT_API_KEY,
) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key)


def normalize_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        return "http://127.0.0.1:1234/v1"
    if normalized.endswith("/v1"):
        return normalized
    if normalized.endswith("/api/v1"):
        return normalized
    return f"{normalized}/v1"


def lm_studio_base_url_candidates(preferred: str | None = None) -> list[str]:
    candidates: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        for part in value.split(","):
            normalized = normalize_base_url(part)
            if normalized not in candidates:
                candidates.append(normalized)

    add(preferred)
    add(os.getenv("TORCS_AI_BASE_URL"))
    add("http://127.0.0.1:1234/v1")
    add("http://localhost:1234/v1")
    return candidates


def _http_json_get(url: str, api_key: str, timeout: float) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = request.Request(url, headers=headers, method="GET")
    with request.urlopen(req, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def list_openai_compatible_models(
    base_url: str,
    api_key: str = DEFAULT_API_KEY,
    timeout: float = 5.0,
) -> list[str]:
    models_url = f"{normalize_base_url(base_url)}/models"
    payload = _http_json_get(models_url, api_key=api_key, timeout=timeout)
    data = payload.get("data", [])
    models: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id", "")).strip()
        if model_id:
            models.append(model_id)
    return models


def is_wsl() -> bool:
    if os.getenv("WSL_DISTRO_NAME"):
        return True
    release = platform.uname().release.lower()
    return "microsoft" in release or "wsl" in release


def powershell_text_request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    api_key: str = DEFAULT_API_KEY,
    timeout: int = 20,
) -> str:
    if is_wsl() and method.upper() == "POST":
        return windows_curl_text_request(
            url,
            method=method,
            body=body,
            api_key=api_key,
            timeout=timeout,
        )

    body_json = json.dumps(body, ensure_ascii=True) if body is not None else ""
    ps_method = method.replace("'", "''")
    ps_url = url.replace("'", "''")
    ps_api_key = api_key.replace("'", "''")
    ps_body = body_json.replace("'", "''")

    script = f"""
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'
$method = '{ps_method}'
$uri = '{ps_url}'
$timeout = {int(timeout)}
$apiKey = '{ps_api_key}'
$body = @'
{ps_body}
'@
$headers = @{{ 'Accept' = 'application/json' }}
if ($apiKey) {{
  $headers['Authorization'] = "Bearer $apiKey"
}}
if ($method -eq 'POST') {{
  $headers['Content-Type'] = 'application/json'
  $resp = Invoke-WebRequest -Method Post -Uri $uri -Headers $headers -Body $body -TimeoutSec $timeout
  $content = $resp.Content
}} else {{
  $resp = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers -TimeoutSec $timeout
  $content = $resp | ConvertTo-Json -Depth 100 -Compress
}}
if ([string]::IsNullOrWhiteSpace($content)) {{
  exit 3
}}
$content
"""

    temp_path = None
    try:
        temp_dir = None
        if is_wsl():
            temp_dir = os.getenv("TORCS_POWERSHELL_TEMP_DIR", "/mnt/c/Users/Public")
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8", dir=temp_dir) as temp_file:
            temp_file.write(script)
            temp_path = temp_file.name

        file_arg = temp_path
        if is_wsl():
            file_arg = (
                subprocess.check_output(["wslpath", "-w", temp_path], text=True, timeout=5)
                .strip()
            )

        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", file_arg],
            capture_output=True,
            timeout=timeout + 5,
            check=False,
        )
        stdout = decode_subprocess_output(result.stdout)
        stderr = decode_subprocess_output(result.stderr)
        if result.returncode != 0:
            message = stderr.strip() or stdout.strip() or "unknown PowerShell error"
            raise RuntimeError(message)
        text = stdout.strip()
        if not text:
            raise RuntimeError("PowerShell request returned empty output.")
        return text
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def windows_curl_text_request(
    url: str,
    *,
    method: str = "POST",
    body: dict[str, Any] | None = None,
    api_key: str = DEFAULT_API_KEY,
    timeout: int = 20,
) -> str:
    temp_path = None
    try:
        command = [
            "curl.exe",
            "--silent",
            "--show-error",
            "--max-time",
            str(max(1, int(timeout))),
            "-X",
            method.upper(),
            "-H",
            "Accept: application/json",
        ]
        if api_key:
            command.extend(["-H", f"Authorization: Bearer {api_key}"])

        if body is not None:
            temp_dir = "/mnt/c/Users/Public" if is_wsl() else None
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8", dir=temp_dir) as temp_file:
                json.dump(body, temp_file, ensure_ascii=True)
                temp_path = temp_file.name
            body_arg = temp_path
            if is_wsl():
                body_arg = (
                    subprocess.check_output(["wslpath", "-w", temp_path], text=True, timeout=5)
                    .strip()
                )
            command.extend(["-H", "Content-Type: application/json", "--data-binary", f"@{body_arg}"])

        command.append(url)
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=max(5, int(timeout) + 5),
            check=False,
        )
        stdout = decode_subprocess_output(result.stdout).strip()
        stderr = decode_subprocess_output(result.stderr).strip()
        if result.returncode != 0:
            raise RuntimeError(stderr or stdout or "curl.exe request failed")
        if not stdout:
            raise RuntimeError("curl.exe request returned empty output.")
        return stdout
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def powershell_json_request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    api_key: str = DEFAULT_API_KEY,
    timeout: int = 20,
) -> dict[str, Any]:
    text = powershell_text_request(
        url,
        method=method,
        body=body,
        api_key=api_key,
        timeout=timeout,
    )
    return json.loads(text)


def decode_subprocess_output(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16-le", "gbk", "cp936", "latin-1"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def list_models_via_powershell_localhost(
    api_key: str = DEFAULT_API_KEY,
    timeout: float = 5.0,
) -> list[str]:
    payload = powershell_json_request(
        "http://127.0.0.1:1234/v1/models",
        method="GET",
        api_key=api_key,
        timeout=max(1, int(timeout)),
    )
    data = payload.get("data", [])
    models: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id", "")).strip()
        if model_id:
            models.append(model_id)
    return models


def choose_model_identifier(
    available_models: list[str],
    requested_model: str = "",
    model_hint: str = DEFAULT_MODEL_HINT,
) -> str:
    if not available_models:
        raise RuntimeError("LM Studio is reachable, but no models are visible from /v1/models.")

    requested = requested_model.strip()
    if requested:
        for model in available_models:
            if model == requested:
                return model
        lowered = requested.lower()
        for model in available_models:
            if model.lower() == lowered:
                return model
        for model in available_models:
            if lowered in model.lower():
                return model

    if model_hint:
        for model in available_models:
            if model_hint in model.lower():
                return model

    return available_models[0]


def connect_openai_compatible_model(
    *,
    base_url: str | None = None,
    requested_model: str = DEFAULT_MODEL_NAME,
    api_key: str = DEFAULT_API_KEY,
    model_hint: str = DEFAULT_MODEL_HINT,
    timeout: float = 5.0,
) -> ModelConnection:
    errors: list[str] = []
    for candidate in lm_studio_base_url_candidates(base_url):
        try:
            models = list_openai_compatible_models(candidate, api_key=api_key, timeout=timeout)
            model_name = choose_model_identifier(models, requested_model=requested_model, model_hint=model_hint)
            client = build_openai_client(base_url=candidate, api_key=api_key)
            return ModelConnection(
                client=client,
                base_url=candidate,
                model_name=model_name,
                visible_models=models,
                transport="http",
            )
        except Exception as exc:
            errors.append(f"{candidate} -> {exc}")

    if is_wsl():
        for transport_name in DEFAULT_WSL_TRANSPORTS:
            if transport_name == "powershell":
                try:
                    models = list_models_via_powershell_localhost(api_key=api_key, timeout=timeout)
                    model_name = choose_model_identifier(models, requested_model=requested_model, model_hint=model_hint)
                    return ModelConnection(
                        client=None,
                        base_url="http://127.0.0.1:1234/v1",
                        model_name=model_name,
                        visible_models=models,
                        transport="powershell",
                        bridge_session=None,
                    )
                except Exception as exc:
                    errors.append(f"powershell localhost proxy -> {exc}")
                continue

            if transport_name == "bridge":
                bridge: WindowsRelaySession | None = None
                try:
                    bridge = WindowsRelaySession(api_key=api_key)
                    models_payload = bridge.request_json(
                        "http://127.0.0.1:1234/v1/models",
                        method="GET",
                        timeout=max(1, int(timeout)),
                    )
                    data = models_payload.get("data", [])
                    models: list[str] = []
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        model_id = str(item.get("id", "")).strip()
                        if model_id:
                            models.append(model_id)
                    model_name = choose_model_identifier(models, requested_model=requested_model, model_hint=model_hint)
                    return ModelConnection(
                        client=None,
                        base_url="http://127.0.0.1:1234/v1",
                        model_name=model_name,
                        visible_models=models,
                        transport="bridge",
                        bridge_session=bridge,
                    )
                except Exception as exc:
                    errors.append(f"windows relay bridge -> {exc}")
                    if bridge is not None:
                        bridge.close()

    joined = "; ".join(errors) if errors else "no candidates were tried"
    raise RuntimeError(
        "Unable to connect to a local LM Studio server. "
        "Make sure LM Studio's local server is started and a Granite model is loaded. "
        f"Tried: {joined}"
    )


def print_connection_banner(connection: ModelConnection, feature_name: str) -> None:
    print("=" * 72)
    print(feature_name)
    print(f"LM Studio endpoint: {connection.base_url}")
    print(f"Selected model: {connection.model_name}")
    print(f"Transport: {connection.transport}")
    preview = ", ".join(connection.visible_models[:5])
    if len(connection.visible_models) > 5:
        preview = f"{preview}, +{len(connection.visible_models) - 5} more"
    print(f"Visible models: {preview}")
    print("=" * 72)


def chat_completion_text(
    connection: ModelConnection,
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    if connection.transport == "http":
        if connection.client is None:
            raise RuntimeError("HTTP transport selected but no OpenAI client is available.")
        response = connection.client.chat.completions.create(
            model=connection.model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        return response.choices[0].message.content.strip()

    if connection.transport == "bridge":
        if connection.bridge_session is None:
            raise RuntimeError("Bridge transport selected but no relay session is available.")
        payload = {
            "model": connection.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        response = connection.bridge_session.request_json(
            f"{normalize_base_url(connection.base_url)}/chat/completions",
            method="POST",
            body=payload,
            timeout=max(1, int(timeout)),
        )
        choices = response.get("choices", [])
        if not choices:
            raise RuntimeError("LM Studio returned no choices.")
        message = choices[0].get("message", {})
        content = str(message.get("content", "")).strip()
        if not content:
            raise RuntimeError("LM Studio returned an empty message.")
        return content

    payload = {
        "model": connection.model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    response = powershell_json_request(
        "http://127.0.0.1:1234/v1/chat/completions",
        method="POST",
        body=payload,
        timeout=max(1, int(timeout)),
    )
    choices = response.get("choices", [])
    if not choices:
        raise RuntimeError("LM Studio returned no choices.")
    message = choices[0].get("message", {})
    content = str(message.get("content", "")).strip()
    if not content:
        raise RuntimeError("LM Studio returned an empty message.")
    return content


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None


def clean_for_tts(text: str) -> str:
    return (
        text.replace("*", "")
        .replace("#", "")
        .replace("`", "")
        .replace('"', "")
        .strip()
    )


def normalize_text_key(text: str, max_words: int = 12) -> str:
    chars: list[str] = []
    for char in text.lower():
        if char.isalnum() or char.isspace():
            chars.append(char)
        else:
            chars.append(" ")
    collapsed = " ".join("".join(chars).split())
    if not collapsed:
        return ""
    return " ".join(collapsed.split()[:max_words])


def compact_track_profile(track: list[float]) -> dict[str, float]:
    if len(track) < 19:
        return {
            "left_opening": -1.0,
            "center_opening": -1.0,
            "right_opening": -1.0,
            "tightest_opening": -1.0,
        }
    left = track[0:6]
    center = track[7:12]
    right = track[13:19]
    return {
        "left_opening": round(mean(left), 3),
        "center_opening": round(mean(center), 3),
        "right_opening": round(mean(right), 3),
        "tightest_opening": round(safe_min(track, -1.0), 3),
    }


def compact_opponent_profile(opponents: list[float]) -> dict[str, float]:
    if len(opponents) < 36:
        return {
            "front_gap": 200.0,
            "left_gap": 200.0,
            "right_gap": 200.0,
            "rear_gap": 200.0,
            "nearest_gap": 200.0,
        }
    front = opponents[16:21]
    left = opponents[21:28]
    right = opponents[9:16]
    rear = opponents[0:4] + opponents[32:36]
    nearest = [distance for distance in opponents if distance >= 0]
    return {
        "front_gap": round(safe_min(front, 200.0), 3),
        "left_gap": round(safe_min(left, 200.0), 3),
        "right_gap": round(safe_min(right, 200.0), 3),
        "rear_gap": round(safe_min(rear, 200.0), 3),
        "nearest_gap": round(safe_min(nearest, 200.0), 3),
    }


def speak_text(
    text: str,
    *,
    enabled: bool = False,
    voice: str = "en-us",
    rate: int = 160,
    pulse_server: str = "/mnt/wslg/PulseServer",
) -> None:
    if not enabled or not text:
        return
    clean = clean_for_tts(text)
    if not clean:
        return
    env = {**os.environ, "PULSE_SERVER": pulse_server}
    try:
        subprocess.run(
            ["espeak", "-v", voice, "-s", str(rate), clean],
            capture_output=True,
            timeout=8,
            env=env,
            check=False,
        )
    except Exception:
        pass


def parse_telemetry(csv_line: str) -> dict[str, Any] | None:
    fields = csv_line.split(",")
    if len(fields) < 91:
        return None

    has_z_column = len(fields) >= 92
    opponent_start = 28 if has_z_column else 27
    track_start = opponent_start + 36
    wheel_start = track_start + 19
    focus_start = wheel_start + 4

    try:
        frame = {
            "seq": parse_int(fields[0]),
            "sim_time": parse_float(fields[1]),
            "player": parse_int(fields[2]),
            "lap": parse_int(fields[3]),
            "x": parse_float(fields[4]),
            "y": parse_float(fields[5]),
            "yaw": parse_float(fields[6]),
            "accel_x": parse_float(fields[7]),
            "accel_y": parse_float(fields[8]),
            "steer": parse_float(fields[9]),
            "throttle": parse_float(fields[10]),
            "brake": parse_float(fields[11]),
            "clutch": parse_float(fields[12]),
            "angle": parse_float(fields[13]),
            "cur_lap_time": parse_float(fields[14]),
            "damage": parse_float(fields[15]),
            "dist_from_start": parse_float(fields[16]),
            "dist_raced": parse_float(fields[17]),
            "fuel": parse_float(fields[18]),
            "gear": parse_int(fields[19]),
            "last_lap_time": parse_float(fields[20]),
            "race_pos": parse_int(fields[21]),
            "rpm": parse_float(fields[22]),
            "speed_x": parse_float(fields[23]),
            "speed_y": parse_float(fields[24]),
            "speed_z": parse_float(fields[25]),
            "track_pos": parse_float(fields[26]),
            "z": parse_float(fields[27]) if has_z_column else 0.0,
            "opponents": [parse_float(value, 200.0) for value in fields[opponent_start:track_start]],
            "track": [parse_float(value, -1.0) for value in fields[track_start:wheel_start]],
            "wheel_spin_vel": [parse_float(value) for value in fields[wheel_start:focus_start]],
            "focus": [parse_float(value, -1.0) for value in fields[focus_start : focus_start + 5]],
        }
    except (IndexError, ValueError):
        return None

    if len(frame["opponents"]) != 36 or len(frame["track"]) != 19:
        return None
    return frame


class TelemetryBuffer:
    def __init__(self, udp_port: int, retention_seconds: float) -> None:
        self.udp_port = udp_port
        self.retention_seconds = retention_seconds
        self.buffer: deque[dict[str, Any]] = deque()
        self.lock = threading.Lock()
        self.frame_count = 0
        self._thread: threading.Thread | None = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", udp_port))
        self.sock.settimeout(0.5)

    def start_background(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._collect_forever, daemon=True)
        self._thread.start()

    def _collect_forever(self) -> None:
        print(f"[Collector] Listening UDP:{self.udp_port}")
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
                frame = parse_telemetry(data.decode("utf-8", errors="replace").strip())
                if not frame:
                    continue
                with self.lock:
                    self.buffer.append(frame)
                    self.frame_count += 1
                    cutoff = frame["sim_time"] - self.retention_seconds
                    while self.buffer and self.buffer[0]["sim_time"] < cutoff:
                        self.buffer.popleft()
                    frame_count = self.frame_count
                    buffer_size = len(self.buffer)
                if frame_count % 100 == 0:
                    print(f"[Collector] {frame_count} frames, buffer={buffer_size}")
            except socket.timeout:
                continue
            except Exception as exc:
                print(f"[Collector] Error: {exc}")

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.buffer)


def select_recent_frames(frames: list[dict[str, Any]], window_seconds: float) -> list[dict[str, Any]]:
    if not frames:
        return []
    cutoff = frames[-1]["sim_time"] - window_seconds
    return [frame for frame in frames if frame["sim_time"] >= cutoff]


def _count_threshold_events(values: list[float], threshold: float) -> int:
    count = 0
    above = False
    for value in values:
        current = value > threshold
        if current and not above:
            count += 1
        above = current
    return count


def summarize_frames(frames: list[dict[str, Any]]) -> dict[str, Any]:
    if not frames:
        return {}

    latest = frames[-1]
    first = frames[0]
    speeds = [frame["speed_x"] for frame in frames]
    throttles = [frame["throttle"] for frame in frames]
    brakes = [frame["brake"] for frame in frames]
    steering = [frame["steer"] for frame in frames]
    track_positions = [frame["track_pos"] for frame in frames]
    angles = [frame["angle"] for frame in frames]
    rpms = [frame["rpm"] for frame in frames]
    front_track = [frame["track"][9] for frame in frames if len(frame["track"]) > 9]
    nearest_opponents = [
        min((distance for distance in frame["opponents"] if distance >= 0), default=200.0)
        for frame in frames
    ]

    summary = {
        "frame_count": len(frames),
        "duration": max(0.0, latest["sim_time"] - first["sim_time"]),
        "avg_speed": mean(speeds),
        "max_speed": max(speeds) if speeds else 0.0,
        "min_speed": min(speeds) if speeds else 0.0,
        "avg_throttle": mean(throttles),
        "avg_brake": mean(brakes),
        "avg_rpm": mean(rpms),
        "peak_rpm": max(rpms) if rpms else 0.0,
        "brake_events": _count_threshold_events(brakes, 0.15),
        "throttle_lifts": _count_threshold_events([1.0 - value for value in throttles], 0.7),
        "off_track_moments": sum(1 for value in track_positions if abs(value) > 1.0),
        "edge_pressure_moments": sum(1 for value in track_positions if abs(value) > 0.8),
        "avg_track_pos": mean(track_positions),
        "track_pos_stddev": stddev(track_positions),
        "steering_stddev": stddev(steering),
        "angle_stddev": stddev(angles),
        "front_track_clearance_avg": mean(front_track),
        "front_track_clearance_now": latest["track"][9] if len(latest["track"]) > 9 else -1.0,
        "nearest_opponent_now": nearest_opponents[-1] if nearest_opponents else 200.0,
        "nearest_opponent_window": min(nearest_opponents) if nearest_opponents else 200.0,
        "damage_delta": latest["damage"] - first["damage"],
        "speed_delta": latest["speed_x"] - first["speed_x"],
    }
    return summary


def latest_state_payload(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": frame["seq"],
        "sim_time": round(frame["sim_time"], 3),
        "lap": frame["lap"],
        "cur_lap_time": round(frame["cur_lap_time"], 3),
        "last_lap_time": round(frame["last_lap_time"], 3),
        "speed_x": round(frame["speed_x"], 3),
        "speed_y": round(frame["speed_y"], 3),
        "speed_z": round(frame["speed_z"], 3),
        "gear": frame["gear"],
        "rpm": round(frame["rpm"], 1),
        "throttle": round(frame["throttle"], 3),
        "brake": round(frame["brake"], 3),
        "steer": round(frame["steer"], 3),
        "track_pos": round(frame["track_pos"], 3),
        "angle": round(frame["angle"], 3),
        "damage": round(frame["damage"], 3),
        "fuel": round(frame["fuel"], 3),
        "race_pos": frame["race_pos"],
        "dist_from_start": round(frame["dist_from_start"], 3),
    }
