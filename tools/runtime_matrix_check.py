#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8880"
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
    if api_ok and combo_ok:
        print("All checks passed.")
        return 0
    print("One or more checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
