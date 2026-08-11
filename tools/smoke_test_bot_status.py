#!/usr/bin/env python3
"""Black-box smoke test for ai_bot.py's two midware clients --
`BotStatusReporter` and `GraniteStrategist` -- against a *real*
`python -m midware.app` process over real HTTP. No in-process mocking of
`urllib.request.urlopen`, no TestClient: this is the companion to
tests/bot/test_status_reporter.py and
tests/bot/test_granite_strategist_runtime.py (which verify the same client
classes with the network mocked out) -- this script instead verifies the
real wire format the two sides actually agree on, the way a real
`ai_bot.py --bot --granite` process would.

It does not need real TORCS or a real Granite/LM Studio: TORCS is not
involved at all (BotStatusReporter and GraniteStrategist are pure HTTP
clients, independent of the SCR/UDP connection), and a tiny fake
OpenAI-compatible /chat/completions endpoint stands in for Granite, exactly
like tools/smoke_test_commentary_queue.py does for commentary.

The midware instance is spawned on scratch ports so this never collides
with a real dev instance you might already have running.

Usage:
    .venv/bin/python tools/smoke_test_bot_status.py [-v]

Exit code 0 if every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_bot  # noqa: E402

VERBOSE = False


def log(msg: str) -> None:
    print(msg, flush=True)


def vlog(msg: str) -> None:
    if VERBOSE:
        print(f"    · {msg}", flush=True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# Fake OpenAI-compatible model server
# --------------------------------------------------------------------------

class FakeModelServer:
    """Canned /chat/completions replies. `strategy_sequence` is consumed one
    entry per request (repeating the last entry once exhausted); each entry
    is either a (strategy, reason) pair -> 200 with valid decision JSON, or
    the string "error" -> HTTP 500 (simulates a Granite/LM Studio failure)."""

    def __init__(self, strategy_sequence: list):
        self.port = free_port()
        self._sequence = list(strategy_sequence)
        self._counter = 0
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._make_handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                with outer._lock:
                    idx = min(outer._counter, len(outer._sequence) - 1)
                    entry = outer._sequence[idx]
                    outer._counter += 1
                if entry == "error":
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                strategy, reason = entry
                decision_json = json.dumps({"strategy": strategy, "reason": reason})
                body = json.dumps({"choices": [{"message": {"content": decision_json}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> None:
        self._thread.start()
        vlog(f"fake model server listening on {self.base_url}")

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


# --------------------------------------------------------------------------
# midware.app subprocess
# --------------------------------------------------------------------------

class Midware:
    def __init__(self):
        self.http_port = free_port()
        self.udp_port = free_port()
        self.scr_port = free_port()
        self.proc: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    def start(self) -> None:
        env = os.environ.copy()
        env["TORCS_MIDWARE_PORT"] = str(self.http_port)
        env["TORCS_TELEMETRY_UDP_PORT"] = str(self.udp_port)
        env["TORCS_SCR_UDP_PORT"] = str(self.scr_port)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "midware.app"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        vlog(f"midware.app started (pid {self.proc.pid}) on {self.base_url}")

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

class Reporter:
    def __init__(self):
        self.results: list[tuple[str, bool, str]] = []

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append((name, passed, detail))
        mark = "PASS" if passed else "FAIL"
        suffix = f" -- {detail}" if detail else ""
        log(f"  [{mark}] {name}{suffix}")

    @property
    def all_passed(self) -> bool:
        return all(passed for _, passed, _ in self.results)


async def wait_healthy(client: httpx.AsyncClient, base_url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = await client.get(f"{base_url}/api/health", timeout=2)
            if r.status_code == 200:
                return
        except Exception as e:  # noqa: BLE001 -- just retrying until the server is up
            last_err = e
        await asyncio.sleep(0.3)
    raise RuntimeError(f"midware never became healthy within {timeout}s: {last_err}")


async def poll_until(predicate, timeout: float = 6.0, interval: float = 0.05):
    """Runs `predicate()` (sync, calls into GraniteStrategist.tick under the
    hood) until it returns a truthy value or the timeout elapses. Returns
    the last (truthy or not) result."""
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        await asyncio.sleep(interval)
        result = predicate()
    return result


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

async def scenario_status_reporter_round_trip(client: httpx.AsyncClient, base_url: str, report: Reporter) -> None:
    log("\n[1] BotStatusReporter -> real /api/bot/status round trip")
    reporter = ai_bot.BotStatusReporter(base_url=base_url, interval=0.05)
    try:
        reporter.update(connected=True, strategy="ATTACK", speed_kmh=142.5, gear=4, immediate=True)
        await asyncio.sleep(0.3)  # let the background thread's send land
        r = await client.get(f"{base_url}/api/bot/status")
        status = r.json()["status"]
        ok = (
            status.get("connected") is True
            and status.get("strategy") == "ATTACK"
            and status.get("speed_kmh") == 142.5
        )
        report.record(
            "real POST from BotStatusReporter is visible via GET /api/bot/status",
            ok, f"status={status}",
        )
    finally:
        reporter.close()
        await asyncio.sleep(0.1)

    r = await client.get(f"{base_url}/api/bot/status")
    status = r.json()["status"]
    report.record(
        "close() sends a final disconnected update the server actually receives",
        status.get("connected") is False,
        f"status={status}",
    )


async def scenario_granite_strategist_success(client: httpx.AsyncClient, base_url: str, report: Reporter) -> None:
    log("\n[2] GraniteStrategist -> real /api/bot/strategy -> fake model, successful decision")
    # Scenario-scoped fake model server: interval=0.0 below means GraniteStrategist
    # resubmits a new request on every single tick(), so a server shared across
    # scenarios would have its reply sequence consumed unpredictably fast --
    # each scenario gets its own model + its own counter instead.
    fake_model = FakeModelServer(strategy_sequence=[("ATTACK", "clear track")])
    fake_model.start()
    await client.post(
        f"{base_url}/api/config/api",
        json={"base_url": fake_model.base_url, "api_key": "smoke-test",
              "model": "fake-smoke-model", "temperature": 0.7, "stream": False},
    )
    try:
        strategist = ai_bot.GraniteStrategist(base_url=base_url, interval=0.0)
        sensor_state = {"speed_x": 180.0, "fuel": 60.0, "damage": 0.0, "track_pos": 0.0,
                         "gear": 5, "race_pos": 2, "dist_raced": 3000.0}

        def check():
            strategy, _reason = strategist.tick(sensor_state)
            return strategy == "ATTACK" and not strategist.fallback

        ok = await poll_until(check)
        report.record(
            "real round trip through midware to the fake model resolves to the model's real answer",
            bool(ok),
            f"last_strategy={strategist.last_strategy()!r} fallback={strategist.fallback} error={strategist.last_error!r}",
        )
    finally:
        fake_model.stop()


async def scenario_granite_strategist_failure_isolation(client: httpx.AsyncClient, base_url: str, report: Reporter) -> None:
    log("\n[3] GraniteStrategist survives a real 500 from the model without crashing or losing state")
    # First reply succeeds (establishes a known non-NORMAL baseline), every
    # reply after that is a real HTTP 500 from the fake model.
    fake_model = FakeModelServer(strategy_sequence=[("ATTACK", "clear track"), "error"])
    fake_model.start()
    await client.post(
        f"{base_url}/api/config/api",
        json={"base_url": fake_model.base_url, "api_key": "smoke-test",
              "model": "fake-smoke-model", "temperature": 0.7, "stream": False},
    )
    try:
        strategist = ai_bot.GraniteStrategist(base_url=base_url, interval=0.0)
        sensor_state = {"speed_x": 180.0, "fuel": 60.0, "damage": 0.0}

        def first_ok():
            strategy, _ = strategist.tick(sensor_state)
            return strategy == "ATTACK" and not strategist.fallback

        baseline_ok = await poll_until(first_ok)
        report.record("baseline successful call before inducing the failure", bool(baseline_ok))

        def now_failing():
            strategist.tick(sensor_state)
            return strategist.fallback

        failing_ok = await poll_until(now_failing)
        report.record(
            "a real 500 round trip sets fallback=True instead of raising into the caller",
            bool(failing_ok), f"last_error={strategist.last_error!r}",
        )
        report.record(
            "the strategist keeps returning the last known-good strategy through the failure",
            strategist.last_strategy() == "ATTACK",
            f"last_strategy={strategist.last_strategy()!r}",
        )
    finally:
        fake_model.stop()


async def scenario_bot_feature_disabled(client: httpx.AsyncClient, base_url: str, report: Reporter) -> None:
    log("\n[4] bot feature disabled: both clients must fail closed, not corrupt server state")
    await client.post(f"{base_url}/api/features/enabled", json={"enabled": ["commentary", "engineer", "coach"]})
    await asyncio.sleep(0.1)

    reporter = ai_bot.BotStatusReporter(base_url=base_url, interval=0.05)
    try:
        reporter.update(connected=True, strategy="ATTACK", immediate=True)
        await asyncio.sleep(0.3)
        r = await client.get(f"{base_url}/api/bot/status")
        status = r.json()["status"]
        report.record(
            "a connected=True update while disabled is rejected server-side (409), not applied",
            status.get("connected") is not True,
            f"status={status}",
        )
    finally:
        reporter.close()
        await asyncio.sleep(0.1)

    strategist = ai_bot.GraniteStrategist(base_url=base_url, interval=0.0)

    def disabled_fails():
        strategist.tick({"fuel": 50.0, "damage": 0.0})
        return strategist.fallback

    disabled_ok = await poll_until(disabled_fails, timeout=4.0)
    report.record(
        "strategy requests while the bot feature is disabled fail closed (fallback), not crash",
        bool(disabled_ok), f"last_error={strategist.last_error!r}",
    )

    # restore for cleanliness, in case anything runs after this scenario
    await client.post(f"{base_url}/api/features/enabled", json={"enabled": ["commentary", "engineer", "coach", "bot"]})


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

async def run() -> bool:
    midware = Midware()
    midware.start()

    report = Reporter()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            log(f"waiting for midware.app on {midware.base_url} ...")
            try:
                await wait_healthy(client, midware.base_url)
            except RuntimeError:
                if midware.proc and midware.proc.stdout:
                    output = midware.proc.stdout.read()
                    log("midware never came up -- captured stdout/stderr:\n" + output)
                raise
            log("midware is up. configuring...")

            await client.post(
                f"{midware.base_url}/api/features/enabled",
                json={"enabled": ["commentary", "engineer", "coach", "bot"]},
            )

            await scenario_status_reporter_round_trip(client, midware.base_url, report)
            await scenario_granite_strategist_success(client, midware.base_url, report)
            await scenario_granite_strategist_failure_isolation(client, midware.base_url, report)
            await scenario_bot_feature_disabled(client, midware.base_url, report)
    finally:
        midware.stop()

    log("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in report.results if ok)
    total = len(report.results)
    log(f"结果: {passed}/{total} passed")
    if not report.all_passed:
        log("失败项:")
        for name, ok, detail in report.results:
            if not ok:
                log(f"  - {name}: {detail}")
    return report.all_passed


def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="print extra diagnostic detail")
    args = parser.parse_args()
    VERBOSE = args.verbose

    try:
        ok = asyncio.run(run())
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
