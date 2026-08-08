"""Integration tests for the Commentary runtime loop: broadcast gating, Granite
failure isolation, WebSocket/TTS fault isolation. Uses the real FastAPI app
(`create_app()`) and a real `/ws` connection; only the model gateway and TTS
HTTP calls are mocked -- see docs/commentary_test_plan.md work package A.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from midware import commentary_engine as ce_module
from midware import runtime
from midware.app import create_app
from midware.commentary_engine import CommentaryConfig

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "evaluation" / "commentary" / "scripts"))
from word_count import count_words  # noqa: E402

ALL_FEATURES = ["commentary", "engineer", "coach", "bot"]


class ControlledClock:
    """Deterministic stand-in for time.time(): each call advances by a fixed
    step. Same idea as tests/unit/test_commentary_modes.py's version --
    duplicated locally to keep this integration test file self-contained."""

    def __init__(self, start: float = 1_000_000.0, step: float = 5.0):
        self.value = start
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class CommentaryRuntimeTestCase(unittest.TestCase):
    """Shared setup: fresh app per test class, commentary enabled, and the
    commentary_engine's transient dedupe state reset so tests don't leak into
    each other (commentary_engine is a module-level singleton shared by every
    test in the process -- see docs/commentary_test_matrix.md note on this)."""

    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(create_app())
        cls.client = cls.context.__enter__()
        cls.client.post("/api/features/enabled", json={"enabled": ALL_FEATURES})

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        runtime.commentary_engine.text_history.clear()
        runtime.commentary_engine.event_history.clear()
        runtime.ctx_mgr.clear_history()


def _drain_until(websocket, msg_type: str, limit: int = 20) -> dict:
    """Read messages off an open TestClient websocket until one of the given
    type arrives (or the read limit is hit); returns that message."""
    for _ in range(limit):
        msg = websocket.receive_json()
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"no {msg_type!r} message received within {limit} reads")


class TestDedupeBeforeBroadcast(CommentaryRuntimeTestCase):
    """Forced regression test #1 (commentary_test_plan.md 5.5): text-level
    dedup must suppress the *broadcast*, not just log a message after the
    fact. Reproduces by triggering two distinct events (different
    event_type/reason, so neither is blocked by the per-signature event
    cooldown -- that cooldown lives in CommentaryEngine.next_decision(), which
    this test deliberately bypasses by calling generate_commentary() directly
    with hand-built payloads) whose mocked model reply is identical."""

    CANNED_REPLY = "Fierce battle for the lead down the back straight!"

    def test_second_identical_reply_is_not_broadcast_to_users(self):
        payload_a = {
            "task": "race_commentary",
            "event_type": "battle",
            "event_reason": "Front gap down to 5.0 m",
            "event_time": 100.0,
            "race_pos": 3,
            "lap": 2,
            "front_gap": 5.0,
        }
        payload_b = {
            "task": "race_commentary",
            "event_type": "pace_surge",
            "event_reason": "Acceleration burst from 90.0 to 120.0 km/h",
            "event_time": 101.0,
            "race_pos": 3,
            "lap": 2,
            "speed_delta": 30.0,
        }

        with patch.object(runtime, "call_ai", AsyncMock(return_value=self.CANNED_REPLY)), \
             patch.object(runtime, "call_tts", AsyncMock(return_value=None)):
            with self.client.websocket_connect("/ws") as websocket:
                self.assertEqual(websocket.receive_json()["type"], "connected")

                asyncio.run(runtime.generate_commentary(event_payload=payload_a, history_mode="summary"))
                first_done = _drain_until(websocket, "ai_done")

                asyncio.run(runtime.generate_commentary(event_payload=payload_b, history_mode="summary"))
                second_done = _drain_until(websocket, "ai_done")

        self.assertEqual(first_done["content"], self.CANNED_REPLY)
        self.assertFalse(first_done.get("duplicate", False))

        # This is the actual regression assertion: the second event's reply
        # is byte-for-byte the same text as the first. A real user consuming
        # `ai_done.content` (not the raw `token` stream -- see
        # commentary_test_matrix.md on index.html/index2.html's token-buffer
        # fallback, which is a separate, already-documented front-end gap)
        # must not see it displayed a second time.
        self.assertNotEqual(
            second_done["content"], self.CANNED_REPLY,
            "duplicate commentary text was broadcast to ai_done a second time "
            "-- should_emit_text() must gate the broadcast, not just log after it",
        )
        self.assertTrue(second_done.get("duplicate", False))


class TestGraniteFailureModes(CommentaryRuntimeTestCase):
    """generate_commentary() broadcasts an 'error' message and re-raises on
    model failure; _run_commentary() (the auto-loop's actual caller) is the
    isolation boundary that swallows it so the loop keeps running."""

    PAYLOAD = {
        "task": "race_commentary",
        "event_type": "battle",
        "event_reason": "Front gap down to 5.0 m",
        "event_time": 200.0,
        "race_pos": 3,
        "lap": 2,
    }

    def test_timeout_is_broadcast_as_error_and_reraised(self):
        with patch.object(runtime, "call_ai", AsyncMock(side_effect=asyncio.TimeoutError("granite timeout"))):
            with self.client.websocket_connect("/ws") as websocket:
                websocket.receive_json()  # connected
                with self.assertRaises(asyncio.TimeoutError):
                    asyncio.run(runtime.generate_commentary(event_payload=self.PAYLOAD))
                error_msg = _drain_until(websocket, "error")
        self.assertIn("timeout", error_msg["message"].lower())

    def test_connection_failure_is_isolated_by_run_commentary(self):
        with patch.object(runtime, "call_ai", AsyncMock(side_effect=ConnectionError("connection refused"))):
            decision = SimpleNamespace(event={"event_type": "battle"}, payload=self.PAYLOAD)
            try:
                asyncio.run(runtime._run_commentary(decision, None, None))
            except Exception as exc:  # pragma: no cover - failure path
                self.fail(f"_run_commentary must isolate model connection failures, raised {exc!r}")

    def test_generic_exception_is_isolated_by_run_commentary(self):
        with patch.object(runtime, "call_ai", AsyncMock(side_effect=RuntimeError("unexpected model error"))):
            decision = SimpleNamespace(event={"event_type": "battle"}, payload=self.PAYLOAD)
            try:
                asyncio.run(runtime._run_commentary(decision, None, None))
            except Exception as exc:  # pragma: no cover - failure path
                self.fail(f"_run_commentary must isolate unexpected model errors, raised {exc!r}")

    def test_empty_response_does_not_raise(self):
        with patch.object(runtime, "call_ai", AsyncMock(return_value="")), \
             patch.object(runtime, "call_tts", AsyncMock(return_value=None)):
            with self.client.websocket_connect("/ws") as websocket:
                websocket.receive_json()  # connected
                try:
                    asyncio.run(runtime.generate_commentary(event_payload=self.PAYLOAD))
                except Exception as exc:  # pragma: no cover - failure path
                    self.fail(f"empty model response must not raise, raised {exc!r}")
                done = _drain_until(websocket, "ai_done")
        self.assertEqual(done["content"], "")


class TestBroadcastIsolation(CommentaryRuntimeTestCase):
    """No WebSocket clients / a broadcast failing for one client must not
    crash the commentary pipeline -- see runtime.py::broadcast L170-178."""

    def test_broadcast_with_no_clients_does_not_raise(self):
        runtime.ws_clients.clear()
        try:
            asyncio.run(runtime.broadcast({"type": "probe", "source": "test"}))
        except Exception as exc:  # pragma: no cover - failure path
            self.fail(f"broadcast() with zero clients must not raise, raised {exc!r}")

    def test_one_failing_client_does_not_block_delivery_to_others(self):
        class FailingClient:
            async def send_json(self, msg):
                raise RuntimeError("simulated dead socket")

        fake = FailingClient()
        runtime.ws_clients.add(fake)
        try:
            with self.client.websocket_connect("/ws") as websocket:
                websocket.receive_json()  # connected
                # "test" is not in ALLOWED_SOURCES (midware/shared/output_bus.py)
                # and would be silently rewritten to "system" by
                # normalize_outbound_message() -- use a real source so this
                # assertion actually checks broadcast() delivery, not that
                # rewrite.
                asyncio.run(runtime.broadcast({"type": "probe", "source": "commentary"}))
                probe = _drain_until(websocket, "probe")
                self.assertEqual(probe["source"], "commentary")
            self.assertNotIn(fake, runtime.ws_clients)
        finally:
            runtime.ws_clients.discard(fake)


class TestTtsFailureIsolation(CommentaryRuntimeTestCase):
    """TTS failure must not break the caption path (RT-10)."""

    def test_call_tts_swallows_http_exceptions(self):
        runtime.tts_config["enabled"] = True
        try:
            with patch("httpx.AsyncClient.post", AsyncMock(side_effect=RuntimeError("network down"))):
                result = asyncio.run(runtime.call_tts("hello"))
            self.assertIsNone(result)
        finally:
            runtime.tts_config["enabled"] = False

    def test_tts_failure_does_not_block_caption_broadcast(self):
        payload = {
            "task": "race_commentary",
            "event_type": "off_track",
            "event_reason": "Car ran wide over the right edge",
            "event_time": 300.0,
            "race_pos": 3,
            "lap": 2,
        }
        runtime.tts_config["enabled"] = True
        try:
            with patch.object(runtime, "call_ai", AsyncMock(return_value="Caption text survives TTS failure")), \
                 patch("httpx.AsyncClient.post", AsyncMock(side_effect=RuntimeError("network down"))):
                with self.client.websocket_connect("/ws") as websocket:
                    websocket.receive_json()  # connected
                    asyncio.run(runtime.generate_commentary(event_payload=payload))
                    done = _drain_until(websocket, "ai_done")
        finally:
            runtime.tts_config["enabled"] = False
        self.assertEqual(done["content"], "Caption text survives TTS failure")


class TestIllegalConfigViaRestApi(CommentaryRuntimeTestCase):
    """5.4 'illegal config produces an explicit error' -- pins the REAL,
    currently-mixed behaviour (see docs/commentary_test_matrix.md) rather
    than asserting a friendlier contract the code doesn't implement."""

    def test_non_numeric_value_raises_uncaught_value_error(self):
        # No validation happens before float(value) in
        # CommentaryEngine.update_config(); under TestClient's default
        # raise_server_exceptions=True this surfaces directly as a
        # ValueError here. In a real deployment uvicorn's own error
        # handling turns the same unhandled exception into a generic 500
        # with no actionable message (confirmed manually against a
        # TestClient(..., raise_server_exceptions=False) instance).
        with self.assertRaises(ValueError):
            self.client.post("/api/commentary/config", json={"baseline_interval": "not-a-number"})
        self.client.post("/api/commentary/config", json={"baseline_interval": 10.0})

    def test_unknown_mode_string_is_silently_accepted(self):
        response = self.client.post("/api/commentary/config", json={"mode": "not_a_real_mode"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["mode"], "not_a_real_mode")
        self.client.post("/api/commentary/config", json={"mode": "hybrid"})


class TestMaxWordsIsPromptHintOnly(CommentaryRuntimeTestCase):
    """Forced regression test #2 (commentary_test_plan.md 5.5): measures
    real output against the word-count function in
    evaluation/commentary/scripts/word_count.py rather than assuming
    max_words is enforced. See docs/commentary_test_matrix.md section 4 for
    the full trace of why it currently is not -- this test documents that
    gap as a measurement, not a pass/fail assertion against a target, per
    the task book's explicit "do not default-truncate" instruction."""

    def test_reply_far_over_max_words_is_broadcast_unmodified(self):
        long_reply = " ".join(f"word{i}" for i in range(80))
        payload = {
            "task": "race_commentary",
            "event_type": "pace_surge",
            "event_reason": "Acceleration burst",
            "event_time": 400.0,
            "race_pos": 2,
            "lap": 3,
        }
        with patch.object(runtime, "call_ai", AsyncMock(return_value=long_reply)), \
             patch.object(runtime, "call_tts", AsyncMock(return_value=None)):
            with self.client.websocket_connect("/ws") as websocket:
                websocket.receive_json()  # connected
                asyncio.run(runtime.generate_commentary(event_payload=payload))
                done = _drain_until(websocket, "ai_done")

        broadcast_word_count = count_words(done["content"])
        configured_limit = CommentaryConfig().max_words
        self.assertEqual(broadcast_word_count, 80)
        self.assertGreater(
            broadcast_word_count, configured_limit,
            "expected this to demonstrate the known gap (no enforcement) -- "
            "if this now fails, max_words enforcement was added and "
            "docs/commentary_test_matrix.md section 4 needs updating",
        )


class TestHighFrequencyEvents(CommentaryRuntimeTestCase):
    """5.4 'high-frequency events must not produce unhandled exceptions or
    unbounded task pile-up'. Drives the real _auto_commentary_loop with a
    fake telemetry store that hands back a new triggerable event on every
    poll and a deliberately slow generate_commentary, so the loop's
    cancel-the-current-task-before-starting-the-next pattern
    (runtime.py::_auto_commentary_loop L504-513) is actually exercised.

    The engine's *global* wall-clock cooldown (real time.time(), default
    1.0s -- see TestGlobalWallClockCooldown) would otherwise rate-limit new
    decisions to ~1/s all by itself and never let two generations overlap
    in a few-second test window, so it's neutralised here with the same
    ControlledClock used in test_commentary_modes.py. What's actually being
    exercised is the realistic production scenario: Granite generation
    (15-30s per docs/commentary-loop.md) frequently outlasts a single
    cooldown window, so a fresh cooldown-cleared event routinely arrives
    while the previous one is still generating."""

    def setUp(self):
        super().setUp()
        # This test is specifically about interrupt mode's cancel-and-replace
        # behaviour, not whichever mode CommentaryConfig defaults to -- pin it
        # explicitly (commentary_engine is a module-level singleton, see
        # CommentaryRuntimeTestCase's docstring, so this must be undone too).
        runtime.commentary_engine.config.interrupt_mode = "interrupt"

    def tearDown(self):
        runtime.commentary_engine.config.interrupt_mode = CommentaryConfig().interrupt_mode

    def test_rapid_events_are_superseded_not_queued(self):
        calls_started = []
        calls_completed = []

        async def slow_generate(t, r, event_payload=None, history_mode="full", request_id=None):
            calls_started.append(event_payload["event_type"])
            # Longer than the loop's own 0.5s poll interval, so a task
            # started on tick N is still running when tick N+1 arrives.
            await asyncio.sleep(0.6)
            calls_completed.append(event_payload["event_type"])
            return "ok"

        frame_log = []

        class FakeTelemetryStore:
            def latest(self):
                return ({"sim_time": 0.0}, [])

            def recent_frames(self, window_seconds):
                return list(frame_log)

        async def drive():
            with patch.object(runtime, "telemetry_store", FakeTelemetryStore()), \
                 patch.object(runtime, "generate_commentary", slow_generate), \
                 patch.object(runtime.runtime_manager, "is_enabled", return_value=True), \
                 patch.object(runtime.commentary_engine.config, "mode", "event"), \
                 patch.object(runtime.commentary_engine.config, "window_seconds", 20.0), \
                 patch.object(ce_module.time, "time", ControlledClock(step=5.0)):
                runtime.commentary_engine.last_lap = -1
                runtime.commentary_engine.event_history.clear()
                runtime.commentary_engine.last_event_wall_clock = 0.0

                loop_task = asyncio.create_task(runtime._auto_commentary_loop())
                try:
                    base = {
                        "sim_time": 0.0, "lap": 1, "speed_x": 40.0, "gear": 3,
                        "track_pos": 0.0, "damage": 0.0, "race_pos": 5, "fuel": 50.0,
                        "throttle": 0.2, "brake": 0.0, "steer": 0.0, "angle": 0.0,
                        "rpm": 5000.0, "dist_from_start": 0.0, "cur_lap_time": 0.0,
                        "last_lap_time": 0.0,
                    }
                    frame_log.append(base)
                    await asyncio.sleep(0.05)
                    # Feed a genuinely new event (lap increments => a fresh
                    # lap_complete signature every time) faster than
                    # slow_generate finishes, for ~2.5s of real time.
                    for i in range(2, 8):
                        frame_log.append({**base, "sim_time": i * 0.4, "lap": i})
                        await asyncio.sleep(0.4)
                    await asyncio.sleep(0.8)
                finally:
                    loop_task.cancel()
                    try:
                        await loop_task
                    except asyncio.CancelledError:
                        pass

        asyncio.run(drive())

        self.assertGreaterEqual(len(calls_started), 2, "expected several events to reach generate_commentary")
        self.assertLess(
            len(calls_completed), len(calls_started),
            "expected at least one in-flight generation to be superseded (cancelled) "
            "by a later higher/equal-priority event rather than queueing behind it",
        )
        self.assertTrue(runtime._commentary_task is None or runtime._commentary_task.done())


if __name__ == "__main__":
    unittest.main()
