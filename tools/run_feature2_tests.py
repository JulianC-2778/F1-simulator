from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "tmp" / "feature2-service-smoke.log"

PYTEST_TARGETS = [
    "tests/unit/test_feature2_core.py",
    "tests/unit/test_coach_prebrief.py",
    "tests/integration/test_feature_apis.py::FeatureApiIntegrationTests::test_coach_prebrief_and_dashboard_expose_lookahead",
    "tests/integration/test_feature_apis.py::FeatureApiIntegrationTests::test_coach_prebrief_parses_fenced_model_json",
]


def run_command(command: list[str], *, env: dict[str, str] | None = None) -> int:
    print("\n>>> " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, env=env).returncode


def request_json(method: str, url: str, payload: dict | None = None, *, timeout: float = 3.0) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_health(base_url: str, *, timeout_seconds: float = 30.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            request_json("GET", f"{base_url}/api/health", timeout=2.0)
            return True
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.5)
    return False


def fake_feature2_frame() -> dict[str, str]:
    frame = {
        "seq": "5202",
        "sim_time": "14.0",
        "lap": "2",
        "speedX": "180.0",
        "speedY": "0.0",
        "speedZ": "0.0",
        "rpm": "8200",
        "gear": "5",
        "fuel": "28.0",
        "damage": "4.0",
        "trackPos": "0.1",
        "curLapTime": "42.5",
        "lastLapTime": "89.1",
        "racePos": "3",
        "throttle": "0.82",
        "brake": "0.0",
        "steer": "0.02",
        "angle": "0.0",
        "distFromStart": "390.0",
        "distRaced": "3990.0",
    }
    frame.update({f"track_{index}": "80" for index in range(19)})
    frame.update({f"opponent_{index}": "200" for index in range(36)})
    return frame


def run_service_smoke(args: argparse.Namespace) -> int:
    base_url = f"http://127.0.0.1:{args.port}"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["TORCS_MIDWARE_HOST"] = "127.0.0.1"
    env["TORCS_MIDWARE_PORT"] = str(args.port)
    env["TORCS_TELEMETRY_UDP_PORT"] = str(args.udp_port)

    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "midware.app"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    try:
        if not wait_for_health(base_url):
            print(f"FAIL: midware did not become healthy at {base_url}. Log: {LOG_PATH}")
            print_tail(LOG_PATH)
            return 1

        request_json("POST", f"{base_url}/api/features/enabled", {"enabled": ["commentary", "engineer", "coach", "bot"]})
        request_json("POST", f"{base_url}/api/telemetry/push", {"telemetry": fake_feature2_frame()})
        dashboard = request_json(
            "GET",
            f"{base_url}/api/coach/dashboard?track_id=default-road&driver_style=late_braker&road_condition=low_grip",
        )

        if not dashboard.get("status", {}).get("has_telemetry"):
            print("FAIL: dashboard did not report live telemetry")
            return 1
        if not dashboard.get("guidance"):
            print("FAIL: dashboard did not return Feature 2 guidance")
            return 1
        if not dashboard.get("lookahead_plan"):
            print("FAIL: dashboard did not return a lookahead plan")
            return 1

        guidance = dashboard["guidance"]
        print(
            "PASS: service smoke "
            f"state={guidance.get('state_id')} "
            f"priority={guidance.get('priority')} "
            f"headline={guidance.get('headline')!r}"
        )
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def print_tail(path: Path, lines: int = 30) -> None:
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in content[-lines:]:
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the focused Feature 2 test suite.")
    parser.add_argument("--service", action="store_true", help="also start midware and run an HTTP dashboard smoke test")
    parser.add_argument("--port", type=int, default=8880, help="midware HTTP port for --service")
    parser.add_argument("--udp-port", type=int, default=3101, help="telemetry UDP port for --service")
    args = parser.parse_args()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    rc = run_command([sys.executable, "-m", "pytest", *PYTEST_TARGETS, "-q"], env=env)
    if rc != 0:
        print("\nInstall test dependencies with: python -m pip install -r requirements-core.txt")
        return rc

    if args.service:
        return run_service_smoke(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
