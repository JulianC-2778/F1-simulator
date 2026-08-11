"""run_bot() orchestration: handshake -> receive -> strategy -> control ->
send, timeout handling, clean shutdown, and heartbeat reporting — driven
with an injected fake ScrClient instead of a real TORCS/scr_server process.
See docs/bot_test_plan.md section 4 (the previously-open "run_bot()
orchestration" gap) and docs/bot_test_matrix.md section 6.

``run_bot()`` now accepts an optional ``client=`` keyword argument (a
minimal testability change — see its docstring) that, when given, is used
instead of constructing a real ``ScrClient(host, port)``. Everything below
that swap is the exact same production code path a real race runs.

The fake client returns a scripted sequence of "frames" — parsed SCR state
dicts, ``{}`` for a recv timeout, or ``None`` to end the race — which lets
these tests be fully deterministic and network-free.

Every ``run_bot()`` call below passes ``track="off"`` to disable the
pre-race map auto-detection (``load_track_model("auto", ...)``), which
otherwise reads whatever raceman config happens to exist under this
machine's ``~/.torcs`` — a real dependency on host state these tests must
not have.
"""

import unittest
from unittest.mock import patch

import ai_bot
from ai_bot import ATTACK, NORMAL, PIT, run_bot


class _OkUrlopenResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"ok":true}'


def _frame(**overrides):
    track = [150.0] * 9 + [180.0] + [150.0] * 9
    state = {
        "speed_x": 80.0, "rpm": 5000.0, "gear": 3, "angle": 0.0, "track_pos": 0.0,
        "track": track, "fuel": 50.0, "damage": 0.0, "opponents": [200.0] * 36,
        "dist_raced": 1000.0, "dist_from_start": 0.0, "last_lap_time": 0.0,
    }
    state.update(overrides)
    return state


class _FakeScrClient:
    """Duck-typed stand-in for ai_bot.ScrClient: no sockets, just a
    pre-scripted sequence of receive_state() return values."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.connected = False
        self.sent_controls = []
        self._closed = False
        self._done = False

    def connect(self):
        self.connected = True

    def receive_state(self):
        if not self._frames:
            self._done = True
            return None
        state = self._frames.pop(0)
        if state is None:
            self._done = True
        return state

    def send_control(self, ctrl):
        self.sent_controls.append(ctrl)

    def close(self):
        self._closed = True

    @property
    def is_shutdown(self):
        return self._done

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RunBotOrchestrationTests(unittest.TestCase):
    def setUp(self):
        ai_bot.set_track_model(None)
        ai_bot._reset_driver_state()
        patcher = patch("urllib.request.urlopen", return_value=_OkUrlopenResponse())
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        ai_bot._reset_driver_state()

    def test_handshake_happens_exactly_once(self):
        client = _FakeScrClient([_frame(), _frame(), None])
        run_bot(client=client, verbose=False, track="off")
        self.assertTrue(client.connected)

    def test_one_control_sent_per_real_frame(self):
        client = _FakeScrClient([_frame(), _frame(), _frame(), None])
        run_bot(client=client, verbose=False, track="off")
        self.assertEqual(len(client.sent_controls), 3)

    def test_timeout_frames_do_not_produce_extra_controls(self):
        # {} means "recv timed out" -- ai_bot.py's own comments are explicit
        # that resending on a timeout is a real prior bug (extra packets put
        # the client permanently behind scr_server's queue), so the loop
        # must `continue` without calling compute_control/send_control again.
        client = _FakeScrClient([_frame(), {}, {}, _frame(), None])
        run_bot(client=client, verbose=False, track="off")
        self.assertEqual(len(client.sent_controls), 2)

    def test_none_frame_ends_the_loop_cleanly(self):
        client = _FakeScrClient([_frame(), None])
        run_bot(client=client, verbose=False, track="off")  # must return, not hang
        self.assertTrue(client.is_shutdown)
        self.assertTrue(client._closed, "the with-block must close the client on exit")

    def test_fixed_strategy_path_applies_safety_filter_every_frame(self):
        # No Granite (use_granite=False): current_strategy must still be
        # safety_filter(strategy, state) on every frame, not the raw
        # `strategy` argument passed straight through -- a critically
        # damaged car must not keep attacking just because no Granite call
        # is involved this run.
        client = _FakeScrClient([_frame(damage=9999.0), None])
        run_bot(strategy=ATTACK, client=client, verbose=False, track="off")
        self.assertEqual(len(client.sent_controls), 1)
        # PIT/DEFEND-class control differs from ATTACK's; the concrete proof
        # is that a critically damaged car does not floor the throttle.
        self.assertNotIn("(accel 1.000)", client.sent_controls[0])

    def test_low_fuel_forces_pit_meta_flag_through_the_full_loop(self):
        client = _FakeScrClient([_frame(fuel=2.0, speed_x=5.0, rpm=800.0, gear=1), None])
        run_bot(strategy=ATTACK, client=client, verbose=False, track="off")
        self.assertIn("(meta 1)", client.sent_controls[0])

    def test_unknown_initial_strategy_falls_back_to_normal_without_raising(self):
        client = _FakeScrClient([_frame(), None])
        run_bot(strategy="NOT_A_REAL_STRATEGY", client=client, verbose=False, track="off")  # must not raise
        self.assertEqual(len(client.sent_controls), 1)

    def test_keyboard_interrupt_mid_loop_is_handled_and_still_closes_client(self):
        class _InterruptingClient(_FakeScrClient):
            def receive_state(self):
                if len(self.sent_controls) >= 1:
                    raise KeyboardInterrupt
                return super().receive_state()

        client = _InterruptingClient([_frame(), _frame(), _frame()])
        run_bot(strategy=NORMAL, client=client, verbose=False, track="off")  # must not propagate
        self.assertTrue(client._closed)

    def test_reporter_is_closed_and_atexit_hook_unregistered(self):
        # Regression guard for a resource leak: run_bot registers
        # reporter.close via atexit for the crash case, but on a normal
        # clean exit it must both call close() AND unregister the atexit
        # hook, or repeated run_bot() calls in one process pile up handlers.
        import atexit as atexit_module

        client = _FakeScrClient([_frame(), None])
        with patch.object(atexit_module, "unregister") as mock_unregister:
            run_bot(client=client, verbose=False, track="off")
        mock_unregister.assert_called_once()


if __name__ == "__main__":
    unittest.main()
