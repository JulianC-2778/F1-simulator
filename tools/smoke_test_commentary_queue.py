#!/usr/bin/env python3
"""Black-box smoke test for the commentary `interrupt_mode` ("interrupt" vs
"queue") feature: hits a *real* `python -m midware.app` process over real
HTTP + WebSocket -- no in-process TestClient, no mocking of runtime.py
internals. This is a companion to the pytest suite
(tests/integration/test_commentary_queue_mode.py), which verifies the same
behaviour by calling internal functions directly; this script instead
verifies it from the outside, the way a real frontend would.

It does not need a real LM Studio/Granite server: it starts its own tiny
fake OpenAI-compatible `/chat/completions` endpoint (instant/fixed replies,
optionally with a deliberate response delay to create an observable "still
generating" window) and points the midware instance at it via
`/api/config/api`. TTS is left disabled, so the script only has to reason
about text broadcasts (`event_detected`/`user_msg`/`ai_start`/`ai_done`),
not audio.

The midware instance is spawned on scratch ports (picked freely, passed via
env vars) so this never collides with a real dev instance you might already
have running.

Usage:
    .venv/bin/python tools/smoke_test_commentary_queue.py [-v]

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
import websockets

ROOT = Path(__file__).resolve().parent.parent

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
    """Instant (or artificially delayed) canned replies for POST .../chat/completions
    -- non-streaming only (the script forces `stream: false` via /api/config/api),
    so this doesn't need to emit SSE."""

    def __init__(self, reply_delay: float = 0.0):
        self.port = free_port()
        self.reply_delay = reply_delay
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
                if outer.reply_delay:
                    time.sleep(outer.reply_delay)
                with outer._lock:
                    outer._counter += 1
                    n = outer._counter
                body = json.dumps(
                    {"choices": [{"message": {"content": f"Fake commentary reply #{n}."}}]}
                ).encode()
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
        vlog(f"fake model server listening on {self.base_url} (reply_delay={self.reply_delay}s)")

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

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.http_port}/ws"

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


# --------------------------------------------------------------------------
# Telemetry + WebSocket helpers
# --------------------------------------------------------------------------

def frame(*, sim_time: float, lap: int, race_pos: int, damage: float, speed_x: float = 40.0,
          throttle: float = 0.2, track_pos: float = 0.0) -> dict:
    return {
        "sim_time": sim_time, "lap": lap, "race_pos": race_pos, "damage": damage,
        "track_pos": track_pos, "speed_x": speed_x, "throttle": throttle,
        "brake": 0.0, "steer": 0.0, "gear": 3, "rpm": 5000.0, "fuel": 80.0,
        "cur_lap_time": 0.0, "last_lap_time": 0.0, "dist_from_start": sim_time * 50.0,
        "angle": 0.0,
    }


async def push_frame(client: httpx.AsyncClient, base_url: str, t: dict) -> None:
    r = await client.post(f"{base_url}/api/telemetry/push", json={"telemetry": t, "rankings": []})
    r.raise_for_status()
    vlog(f"pushed telemetry: lap={t['lap']} race_pos={t['race_pos']} damage={t['damage']} speed_x={t['speed_x']}")


async def recv_until(ws, msg_type: str, timeout: float = 6.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out after {timeout}s waiting for a {msg_type!r} message")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        msg = json.loads(raw)
        vlog(f"ws <- {msg.get('type')} {msg.get('event', {}).get('event_type', '')}")
        if msg.get("type") == msg_type:
            return msg


async def assert_silent(ws, forbidden_types: set[str], seconds: float) -> None:
    """Read whatever arrives for `seconds`; raise if any message of a
    forbidden type shows up (used to prove a queued item stayed queued)."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return
        msg = json.loads(raw)
        vlog(f"ws <- {msg.get('type')} (during silence check)")
        if msg.get("type") in forbidden_types:
            raise AssertionError(f"unexpected {msg['type']!r} arrived: {msg}")


async def configure_commentary(client: httpx.AsyncClient, base_url: str, **fields) -> None:
    r = await client.post(f"{base_url}/api/commentary/config", json=fields)
    r.raise_for_status()


async def playback_done(client: httpx.AsyncClient, base_url: str) -> None:
    r = await client.post(f"{base_url}/api/commentary/playback_done", json={})
    r.raise_for_status()


async def settle_engine_state(client: httpx.AsyncClient, base_url: str) -> None:
    """CommentaryEngine.next_decision special-cases its very first call
    (`self.last_lap == -1`) to silently seed internal state and return None
    -- no broadcast happens for it. Push one baseline frame and sleep past a
    0.5s loop tick so that freebie is consumed before the real scenarios."""
    await push_frame(client, base_url, frame(sim_time=0.0, lap=1, race_pos=5, damage=0.0))
    await asyncio.sleep(0.8)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

async def scenario_queue_holds_back(client, ws, base_url, report: Reporter) -> None:
    log("\n[1] queue 模式：忙碌期间新事件应排队等待，不能提前广播")
    await configure_commentary(client, base_url, mode="event", interrupt_mode="queue")
    await settle_engine_state(client, base_url)

    await push_frame(client, base_url, frame(sim_time=10.0, lap=2, race_pos=5, damage=0.0))
    ev1 = await recv_until(ws, "event_detected")
    done1 = await recv_until(ws, "ai_done")
    report.record(
        "first event (not busy) generates live",
        ev1["event"]["event_type"] == "lap_complete" and bool(done1["content"]),
        f"event_type={ev1['event']['event_type']!r}",
    )

    await asyncio.sleep(0.6)  # let a loop tick (0.5s) observe the previous push
    t_push2 = time.monotonic()
    await push_frame(client, base_url, frame(sim_time=11.0, lap=2, race_pos=4, damage=0.0))
    try:
        await assert_silent(ws, {"event_detected", "ai_start", "ai_done"}, seconds=2.5)
        report.record("second event held back while the first is still 'playing'", True)
    except AssertionError as e:
        report.record("second event held back while the first is still 'playing'", False, str(e))

    await playback_done(client, base_url)
    ev2 = await recv_until(ws, "event_detected", timeout=6)
    done2 = await recv_until(ws, "ai_done", timeout=6)
    elapsed = time.monotonic() - t_push2
    report.record(
        "second event released only after playback_done",
        ev2["event"]["event_type"] == "position_change" and bool(done2["content"]),
        f"event_type={ev2['event']['event_type']!r}, released {elapsed:.2f}s after being pushed",
    )
    await playback_done(client, base_url)  # close item 2 out too, back to fully idle


async def scenario_single_slot_priority(client, ws, base_url, report: Reporter) -> None:
    log("\n[2] 单槽队列：只保留最新/最高优先级一条，低优先级的会被丢弃")
    await asyncio.sleep(0.6)
    await push_frame(client, base_url, frame(sim_time=20.0, lap=3, race_pos=4, damage=0.0))
    await recv_until(ws, "event_detected")  # lap_complete, live (not busy)
    await recv_until(ws, "ai_done")

    await asyncio.sleep(0.6)
    # low priority (3): pace_surge needs a >22 speed jump with throttle > 0.8
    await push_frame(client, base_url, frame(sim_time=21.0, lap=3, race_pos=4, damage=0.0, speed_x=70.0, throttle=0.9))
    await asyncio.sleep(0.8)
    # high priority (5): contact needs a >=5.0 damage jump
    await push_frame(client, base_url, frame(sim_time=22.0, lap=3, race_pos=4, damage=6.0, speed_x=70.0, throttle=0.9))
    await asyncio.sleep(0.8)

    await playback_done(client, base_url)
    released = await recv_until(ws, "event_detected", timeout=6)
    await recv_until(ws, "ai_done", timeout=6)
    ok = released["event"]["event_type"] == "contact"
    report.record(
        "only the higher-priority queued candidate survives",
        ok,
        f"released event_type={released['event']['event_type']!r}"
        if ok else
        f"released event_type={released['event']['event_type']!r}, expected 'contact' -- "
        "'pace_surge' should have been dropped when 'contact' replaced it",
    )
    try:
        await assert_silent(ws, {"event_detected"}, seconds=2.0)
        report.record("the dropped low-priority event never surfaces afterward", True)
    except AssertionError as e:
        report.record("the dropped low-priority event never surfaces afterward", False, str(e))
    await playback_done(client, base_url)


async def scenario_interrupt_mode_cancels(client, ws, base_url, report: Reporter) -> None:
    log("\n[3] interrupt 模式对照：新事件应立即打断，不需要等 playback_done")
    await configure_commentary(client, base_url, interrupt_mode="interrupt")
    await asyncio.sleep(0.6)

    await push_frame(client, base_url, frame(sim_time=30.0, lap=4, race_pos=4, damage=6.0))
    await recv_until(ws, "ai_start")  # generation started -- the fake model is still "thinking"

    t_push2 = time.monotonic()
    await push_frame(client, base_url, frame(sim_time=31.0, lap=4, race_pos=3, damage=6.0))
    ev2 = await recv_until(ws, "event_detected", timeout=4)
    done2 = await recv_until(ws, "ai_done", timeout=4)
    elapsed = time.monotonic() - t_push2
    report.record(
        "second event interrupts the first mid-generation, no playback_done needed",
        ev2["event"]["event_type"] == "position_change" and bool(done2["content"]),
        f"replaced within {elapsed:.2f}s of being pushed",
    )


async def scenario_manual_override(client, ws, base_url, report: Reporter) -> None:
    log("\n[4] 手动触发：无论 queue 模式下有没有排队内容，都应立即打断并清空队列")
    await configure_commentary(client, base_url, interrupt_mode="queue")
    await asyncio.sleep(0.6)

    await push_frame(client, base_url, frame(sim_time=40.0, lap=5, race_pos=3, damage=6.0))
    await recv_until(ws, "event_detected")
    await recv_until(ws, "ai_done")  # busy now, waiting on this item's own playback_done

    await asyncio.sleep(0.6)
    await push_frame(client, base_url, frame(sim_time=41.0, lap=5, race_pos=2, damage=6.0))
    await asyncio.sleep(0.8)  # let it settle into the queue slot as pending

    r = await client.post(
        f"{base_url}/api/commentary/manual",
        json={"prompt": "Manual override smoke-test trigger."},
    )
    report.record("manual trigger endpoint accepts the request", r.status_code == 200 and r.json().get("ok") is True)

    done = await recv_until(ws, "ai_done", timeout=6)
    report.record("manual commentary is broadcast promptly, bypassing the queue", bool(done["content"]))

    await playback_done(client, base_url)
    try:
        await assert_silent(ws, {"event_detected"}, seconds=2.0)
        report.record("the item that was queued got cleared by the manual trigger, never delivered", True)
    except AssertionError as e:
        report.record("the item that was queued got cleared by the manual trigger, never delivered", False, str(e))


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

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


async def run(reply_delay: float) -> bool:
    fake_model = FakeModelServer(reply_delay=reply_delay)
    fake_model.start()

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
            await client.post(
                f"{midware.base_url}/api/config/api",
                json={
                    "base_url": fake_model.base_url, "api_key": "smoke-test",
                    "model": "fake-smoke-model", "temperature": 0.7, "stream": False,
                },
            )
            await client.post(f"{midware.base_url}/api/config/tts", json={"enabled": False})
            # window_seconds gates CommentaryEngine's "recent frames" cutoff by
            # sim_time, not wall clock -- widen it well past this script's max
            # sim_time spread (0..41) so pushes never get pruned out of the
            # window before detect_event() even sees them.
            await client.post(
                f"{midware.base_url}/api/commentary/config",
                # event_cooldown is the *global* wall-clock gate between any two
                # emitted events (CommentaryEngine._can_emit_event) -- default
                # 1.0s. Scenario 3 deliberately pushes a second event within a
                # few hundred ms of the first (to catch it "still generating"),
                # which a 1.0s cooldown would itself reject before the
                # interrupt/queue logic is even reached. Lower it here; the
                # fixed per-event-type EVENT_COOLDOWNS (1.0-2.5s, keyed by
                # sim_time) still apply and are what actually matters for this
                # script, since every push here uses a different event type.
                json={"window_seconds": 120.0, "event_cooldown": 0.05, "dedupe_seconds": 10.0},
            )

            async with websockets.connect(midware.ws_url) as ws:
                hello = json.loads(await ws.recv())
                assert hello.get("type") == "connected", f"unexpected first WS message: {hello}"

                await scenario_queue_holds_back(client, ws, midware.base_url, report)
                await scenario_single_slot_priority(client, ws, midware.base_url, report)
                await scenario_interrupt_mode_cancels(client, ws, midware.base_url, report)
                await scenario_manual_override(client, ws, midware.base_url, report)
    finally:
        midware.stop()
        fake_model.stop()

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
    parser.add_argument("-v", "--verbose", action="store_true", help="print every WS message as it arrives")
    parser.add_argument(
        "--reply-delay", type=float, default=0.4,
        help="artificial delay (s) the fake model waits before replying -- needs to be long "
             "enough to reliably push a second telemetry event while the first is still "
             "generating (scenario 3); default 0.4s",
    )
    args = parser.parse_args()
    VERBOSE = args.verbose

    try:
        ok = asyncio.run(run(args.reply_delay))
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
