#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _load_project_config():
    config_path = Path(__file__).resolve().parent.parent / "config.py"
    spec = importlib.util.spec_from_file_location("f1_simulator_config", config_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load project config: {config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config = _load_project_config()

DEFAULT_BASE_URL = config.MIDWARE_BASE_URL
FEATURES = ("commentary", "engineer", "coach", "bot")


def request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 3.0,
) -> tuple[bool, dict[str, Any] | str]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:240]}"
    except Exception as exc:
        return False, str(exc)

    try:
        return True, json.loads(raw)
    except json.JSONDecodeError:
        return False, f"Non-JSON response: {raw[:240]}"


def feature_combinations() -> list[tuple[str, ...]]:
    combos: list[tuple[str, ...]] = []
    for size in range(2, len(FEATURES) + 1):
        combos.extend(itertools.combinations(FEATURES, size))
    return combos


def check_required_api(base_url: str) -> bool:
    ok = True
    for path in (
        "/api/health",
        "/api/features",
        "/api/features/status",
        "/api/race/snapshot",
        "/api/coach/dashboard",
        "/api/engineer/history",
        "/api/bot/status",
    ):
        success, payload = request_json(base_url, path)
        status = "OK" if success else "FAIL"
        print(f"[{status}] {path}")
        if not success:
            print(f"       {payload}")
            ok = False
    return ok


def check_combinations(base_url: str) -> bool:
    ok = True
    for combo in feature_combinations():
        success, payload = request_json(
            base_url,
            "/api/features/enabled",
            method="POST",
            body={"enabled": list(combo)},
        )
        status = "OK" if success else "FAIL"
        print(f"[{status}] combination={','.join(combo)}")
        if not success:
            print(f"       {payload}")
            ok = False
            continue
        if isinstance(payload, dict):
            enabled = payload.get("combination", {}).get("enabled", [])
            if sorted(enabled) != sorted(combo):
                print(f"       expected {combo}, got {enabled}")
                ok = False
    return ok


def _status_by_name(base_url: str) -> dict[str, dict[str, Any]]:
    success, payload = request_json(base_url, "/api/features/status")
    if not success or not isinstance(payload, dict):
        return {}
    return {
        str(item.get("name")): item
        for item in payload.get("features", [])
        if isinstance(item, dict)
    }


def check_real_feature_gates(base_url: str) -> bool:
    """Verify disabled settings affect handlers, not only returned metadata."""
    initial = _status_by_name(base_url)
    original_enabled = [name for name in FEATURES if initial.get(name, {}).get("enabled", True)]
    probes = {
        "commentary": ("/api/commentary/manual", "POST", {"prompt": "runtime gate probe"}),
        "engineer": ("/api/engineer/ask", "POST", {"question": "runtime gate probe"}),
        "coach": ("/api/coach/dashboard", "GET", None),
        "bot": ("/api/bot/strategy", "POST", {"sensor_state": {}}),
    }
    ok = True
    try:
        for feature in FEATURES:
            enabled = [name for name in FEATURES if name != feature]
            changed, error = request_json(
                base_url,
                "/api/features/enabled",
                method="POST",
                body={"enabled": enabled},
            )
            if not changed:
                print(f"[FAIL] disable {feature}: {error}")
                ok = False
                continue

            path, method, body = probes[feature]
            success, result = request_json(base_url, path, method=method, body=body)
            rejected = not success and str(result).startswith("HTTP 409:")
            states = _status_by_name(base_url)
            state = states.get(feature, {})
            state_ok = state.get("enabled") is False and state.get("active") is False
            passed = rejected and state_ok
            print(f"[{'OK' if passed else 'FAIL'}] gate={feature} http409={rejected} inactive={state_ok}")
            if not passed:
                print(f"       response={result} state={state}")
                ok = False
    finally:
        request_json(
            base_url,
            "/api/features/enabled",
            method="POST",
            body={"enabled": original_enabled},
        )
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Check unified runtime APIs for feature combinations.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="midware base URL")
    parser.add_argument(
        "--skip-combinations",
        action="store_true",
        help="only check API availability, do not POST feature combinations",
    )
    args = parser.parse_args()

    print(f"Runtime matrix check -> {args.base_url}")
    api_ok = check_required_api(args.base_url)
    combo_ok = True if args.skip_combinations else check_combinations(args.base_url)
    gate_ok = check_real_feature_gates(args.base_url)
    if api_ok and combo_ok and gate_ok:
        print("All checks passed.")
        return 0
    print("One or more checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
