"""Integration tests for `interrupt_mode == "queue"` (see docs/commentary_test_plan.md
5.3 modes/priority/pre-emption): a new event no longer cancels the commentary that's
currently generating/playing -- it sits in a single-slot queue, gets generated silently
in the background (prefetch) while the current one is still on screen, and is only
broadcast to clients once the frontend reports `/api/commentary/playback_done`. Manual
triggers are the one exception -- they always interrupt immediately, in every mode.

Companion to tests/integration/test_commentary_runtime.py, which covers the default
`interrupt_mode == "interrupt"` (cancel-and-replace) behaviour; that suite is
untouched and still exercises the pre-existing code path unchanged.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from midware import runtime
from midware.app import create_app
from midware.commentary_engine import CommentaryDecision

ALL_FEATURES = ["commentary", "engineer", "coach", "bot"]


def _decision(event_type: str, priority: int, **payload_extra) -> CommentaryDecision:
    payload = {
        "task": "race_commentary",
        "event_type": event_type,
        "event_reason": event_type,
        "event_time": 0.0,
        "race_pos": 3,
        "lap": 2,
        **payload_extra,
    }
    return CommentaryDecision(event={"event_type": event_type, "reason": event_type, "priority": priority}, payload=payload)


def _drain_until(websocket, msg_type: str, limit: int = 30) -> dict:
    for _ in range(limit):
        msg = websocket.receive_json()
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"no {msg_type!r} message received within {limit} reads")


class QueueModeTestCase(unittest.TestCase):
    """`commentary_engine` and the runtime's queue globals
    (`_commentary_task`/`_playback_busy`/`_pending_decision`/`_prefetch_task`) are
    module-level singletons shared by every test in the `tests/integration` process
    (see test_commentary_runtime.py::CommentaryRuntimeTestCase) -- reset them all
    around each test so nothing leaks into unrelated interrupt-mode tests, which
    assume the default `interrupt_mode == "interrupt"`."""

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
        runtime.commentary_engine.config.interrupt_mode = "interrupt"
        runtime._commentary_task = None
        runtime._commentary_priority = 0
        runtime._playback_busy = False
        runtime._pending_decision = None
        runtime._prefetch_task = None

    def tearDown(self):
        async def _cancel_leftover_tasks():
            for attr in ("_commentary_task", "_prefetch_task"):
                task = getattr(runtime, attr)
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        asyncio.run(_cancel_leftover_tasks())
        runtime.commentary_engine.config.interrupt_mode = "interrupt"
        runtime._commentary_task = None
        runtime._commentary_priority = 0
        runtime._playback_busy = False
        runtime._pending_decision = None
        runtime._prefetch_task = None


class TestInterruptModeConfigApi(QueueModeTestCase):
    def test_get_config_reports_interrupt_mode(self):
        response = self.client.get("/api/commentary/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["interrupt_mode"], "interrupt")

    def test_post_config_switches_to_queue_mode(self):
        response = self.client.post("/api/commentary/config", json={"interrupt_mode": "queue"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["interrupt_mode"], "queue")
        self.assertEqual(runtime.commentary_engine.config.interrupt_mode, "queue")


class TestQueuePrefetchIsSilentUntilRelease(QueueModeTestCase):
    """The whole point of queue mode: the queued item is fully generated (text +
    TTS) *before* it's released, but nothing about it -- not even the system
    "Event: ..." line or the transcript text -- reaches the client until then."""

    def test_queued_event_generates_in_background_without_broadcasting(self):
        runtime.commentary_engine.config.interrupt_mode = "queue"
        with patch.object(runtime, "call_ai", AsyncMock(return_value="Silent draft, not yet shown.")), \
             patch.object(runtime, "call_tts", AsyncMock(return_value=None)):
            with self.client.websocket_connect("/ws") as websocket:
                self.assertEqual(websocket.receive_json()["type"], "connected")

                async def drive():
                    runtime._playback_busy = True  # simulate: something else is already playing
                    await runtime._queue_event(_decision("battle", priority=4))
                    self.assertIsNotNone(runtime._pending_decision)
                    self.assertIsNotNone(runtime._prefetch_task)
                    await runtime._prefetch_task  # let the silent generation finish
                    # Nothing above should have broadcast anything -- prove it by
                    # broadcasting a distinguishable probe and asserting it's the
                    # very next message the client receives.
                    await runtime.broadcast({"type": "probe", "source": "commentary"})

                asyncio.run(drive())
                self.assertEqual(websocket.receive_json()["type"], "probe")


class TestQueueAdvanceReleasesPendingItem(QueueModeTestCase):
    def test_advance_queue_broadcasts_the_queued_item(self):
        runtime.commentary_engine.config.interrupt_mode = "queue"
        with patch.object(runtime, "call_ai", AsyncMock(return_value="Now it is this car's turn.")), \
             patch.object(runtime, "call_tts", AsyncMock(return_value=None)):
            with self.client.websocket_connect("/ws") as websocket:
                websocket.receive_json()  # connected

                async def drive():
                    runtime._playback_busy = True
                    await runtime._queue_event(_decision("contact", priority=5))
                    await runtime._prefetch_task
                    await runtime._advance_queue()
                    await runtime._commentary_task  # the release task _advance_queue scheduled

                asyncio.run(drive())

                event_msg = _drain_until(websocket, "event_detected")
                self.assertEqual(event_msg["event"]["event_type"], "contact")
                done = _drain_until(websocket, "ai_done")
                self.assertEqual(done["content"], "Now it is this car's turn.")

        self.assertIsNone(runtime._pending_decision)
        self.assertTrue(
            runtime._playback_busy,
            "the just-released item is now 'current' -- stays busy until its own playback_done",
        )

    def test_advance_queue_with_nothing_pending_just_clears_busy(self):
        runtime._playback_busy = True
        asyncio.run(runtime._advance_queue())
        self.assertFalse(runtime._playback_busy)
        self.assertIsNone(runtime._commentary_task)


class TestQueueSingleSlotPriority(QueueModeTestCase):
    """Only the newest/highest-priority pending event survives -- see the
    "只保留最新/最高优先级一条" decision in the 2026-08-04 design discussion, chosen
    over FIFO to keep commentary from drifting further and further behind the
    live race as events pile up."""

    def test_lower_priority_event_does_not_replace_pending(self):
        with patch.object(runtime, "call_ai", AsyncMock(return_value="draft")), \
             patch.object(runtime, "call_tts", AsyncMock(return_value=None)):
            async def drive():
                runtime._playback_busy = True
                await runtime._queue_event(_decision("contact", priority=5))
                await runtime._prefetch_task
                await runtime._queue_event(_decision("pace_surge", priority=3))

            asyncio.run(drive())

        self.assertEqual(runtime._pending_decision.event["event_type"], "contact")

    def test_equal_or_higher_priority_event_replaces_pending(self):
        with patch.object(runtime, "call_ai", AsyncMock(return_value="draft")), \
             patch.object(runtime, "call_tts", AsyncMock(return_value=None)):
            async def drive():
                runtime._playback_busy = True
                await runtime._queue_event(_decision("pace_surge", priority=3))
                await runtime._prefetch_task
                await runtime._queue_event(_decision("contact", priority=5))
                await runtime._prefetch_task  # the replacement's own prefetch

            asyncio.run(drive())

        self.assertEqual(runtime._pending_decision.event["event_type"], "contact")


class TestPlaybackDoneEndpointReleasesQueue(QueueModeTestCase):
    def test_playback_done_releases_queued_item_over_websocket(self):
        runtime.commentary_engine.config.interrupt_mode = "queue"
        with patch.object(runtime, "call_ai", AsyncMock(return_value="released via playback_done")), \
             patch.object(runtime, "call_tts", AsyncMock(return_value=None)):
            with self.client.websocket_connect("/ws") as websocket:
                websocket.receive_json()  # connected

                async def enqueue():
                    runtime._playback_busy = True
                    await runtime._queue_event(_decision("lap_complete", priority=4))
                    await runtime._prefetch_task

                asyncio.run(enqueue())

                response = self.client.post("/api/commentary/playback_done", json={"request_id": "whatever"})
                self.assertEqual(response.status_code, 200)

                done = _drain_until(websocket, "ai_done")
                self.assertEqual(done["content"], "released via playback_done")

        self.assertIsNone(runtime._pending_decision)

    def test_playback_done_is_a_harmless_noop_in_interrupt_mode(self):
        # interrupt_mode defaults to "interrupt" here (setUp) -- this must not
        # raise even though queue-only state (_pending_decision) is untouched.
        response = self.client.post("/api/commentary/playback_done", json={})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(runtime._playback_busy)


class TestManualTriggerAlwaysInterrupts(QueueModeTestCase):
    """手动触发的解说永远立即打断，不受 interrupt_mode 的 queue 设置约束
    (2026-08-04 design decision)."""

    def test_manual_trigger_clears_queue_and_broadcasts_immediately(self):
        runtime.commentary_engine.config.interrupt_mode = "queue"
        with patch.object(runtime, "call_ai", AsyncMock(return_value="queued draft")), \
             patch.object(runtime, "call_tts", AsyncMock(return_value=None)):
            async def enqueue():
                runtime._playback_busy = True
                await runtime._queue_event(_decision("battle", priority=4))
                await runtime._prefetch_task

            asyncio.run(enqueue())

        self.assertIsNotNone(runtime._pending_decision)

        with patch.object(runtime, "call_ai", AsyncMock(return_value="Manual override commentary.")), \
             patch.object(runtime, "call_tts", AsyncMock(return_value=None)):
            with self.client.websocket_connect("/ws") as websocket:
                websocket.receive_json()  # connected
                response = self.client.post(
                    "/api/commentary/manual", json={"prompt": "Manual override commentary trigger."}
                )
                self.assertTrue(response.json()["ok"])
                done = _drain_until(websocket, "ai_done")
                self.assertEqual(done["content"], "Manual override commentary.")

        self.assertIsNone(runtime._pending_decision, "manual trigger must clear anything sitting in the queue")


class TestAutoLoopQueuesInsteadOfCancelling(QueueModeTestCase):
    """Mirrors tests/integration/test_commentary_runtime.py::TestHighFrequencyEvents
    but for interrupt_mode == "queue": drives the real _auto_commentary_loop and
    checks that a second event arriving while the first is still generating gets
    queued as a silent prefetch instead of cancelling the in-flight generation."""

    def test_rapid_events_are_queued_not_cancelled(self):
        runtime.commentary_engine.config.interrupt_mode = "queue"
        decisions = [
            _decision("lap_complete", priority=4),
            _decision("battle", priority=4),
            _decision("contact", priority=5),
        ]
        calls_started = []

        async def slow_generate(t, r, event_payload=None, history_mode="full", request_id=None, silent=False):
            calls_started.append((event_payload["event_type"], silent))
            await asyncio.sleep(0.7)
            if silent:
                return {
                    "request_id": request_id or "x", "user_content": "u", "reply": "ok",
                    "is_duplicate": False, "audio": None, "stats": {},
                }
            return "ok"

        class FakeTelemetryStore:
            def latest(self):
                return ({"sim_time": 0.0}, [])

            def recent_frames(self, window_seconds):
                return [{"sim_time": 0.0}]

        async def drive():
            with patch.object(runtime, "telemetry_store", FakeTelemetryStore()), \
                 patch.object(runtime, "generate_commentary", slow_generate), \
                 patch.object(runtime.runtime_manager, "is_enabled", return_value=True), \
                 patch.object(runtime.commentary_engine.config, "mode", "event"), \
                 patch.object(runtime.commentary_engine, "next_decision", side_effect=decisions + [None] * 20):
                loop_task = asyncio.create_task(runtime._auto_commentary_loop())
                try:
                    # 4-5 loop ticks (0.5s poll interval) so all three decisions
                    # above get consumed, plus enough slack for the 0.7s
                    # (silent) generations to actually run.
                    await asyncio.sleep(2.2)
                finally:
                    loop_task.cancel()
                    try:
                        await loop_task
                    except asyncio.CancelledError:
                        pass

        asyncio.run(drive())

        self.assertGreaterEqual(len(calls_started), 2, "expected the live generation plus at least one prefetch")
        self.assertEqual(calls_started[0][1], False, "the first (not-busy) decision must generate live, not silently")
        self.assertTrue(
            any(silent for _, silent in calls_started[1:]),
            "an event arriving while busy must be queued as a silent prefetch, not dropped or cancel the live task",
        )


if __name__ == "__main__":
    unittest.main()
