"""Work package C's control-loop compute-latency measurement --
docs/bot_test_plan.md 6.2's explicit exception: "控制环延迟: 不需要真实
TORCS -- 可以用工作包 A 里已经搭好的 fake ScrClient/直接函数调用循环跑
1000+ 帧, 测量纯计算耗时分布 (u1-u0), 这是纯 CPU 开销, 不受网络影响".

This drives `compute_control` directly (no `ScrClient`, no `scr_server`,
no subprocess) through 1000+ synthetic frames spanning the code paths that
actually run in a real race -- straight-line cruise, cornering/braking,
each strategy family, and side-traffic avoidance -- and asserts the
resulting u1-u0 compute-latency distribution against
docs/bot_test_plan.md 6.4's acceptance targets (median <= 5ms, P95 <= 15ms,
leaving margin under the 20ms `scr_server` simulation step).

This measures `u1_control_computed - u0_scr_state_received` only. The other
two docs/bot_test_plan.md 6.1 chains -- `send_latency`/`frame_latency`
(needs a real UDP round trip) and the Granite RTT chain (`g0`-`g3`, needs a
real midware + Granite round trip) -- are out of scope for this file; see
evaluation/bot/README.md's "Capturing real latency data" section for their
status.
"""

from __future__ import annotations

import math
import statistics
import time
import unittest

import ai_bot
from ai_bot import ATTACK, DEFEND, NORMAL, PIT, SAVE_FUEL, compute_control

# docs/bot_test_plan.md 6.4 (discussion targets, not silently loosened here).
_MEDIAN_BUDGET_S = 0.005
_P95_BUDGET_S = 0.015


def _percentile(values: list[float], p: float) -> float:
    """Same linear-interpolation definition as evaluation/*/scripts/latency_stats.py,
    inlined to keep tests/bot/ independent of evaluation/bot/ imports."""
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    rank = p * (n - 1)
    lower, upper = math.floor(rank), math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (rank - lower) * (ordered[upper] - ordered[lower])


def _frame_states():
    """A representative, deterministic cycle of states covering the main
    compute_control code paths: clear straight, hot-into-corner braking,
    steady cruise-at-cap, and side-traffic avoidance -- the same shapes
    tests/bot/test_control_logic.py and test_traffic_and_launch.py already
    exercise individually, just replayed as a continuous drive here."""
    straight = [150.0] * 9 + [180.0] + [150.0] * 9
    corner = [60.0] * 19
    cruise_track = [200.0] * 19
    clear_opponents = [200.0] * 36

    close_opponents = [200.0] * 36
    close_opponents[22] = 6.0  # right-side traffic, inside _AVOID_RIGHT

    frames = [
        {"speed_x": 80.0, "rpm": 5000.0, "gear": 3, "angle": 0.0, "track_pos": 0.0,
         "track": straight, "opponents": clear_opponents},
        {"speed_x": 250.0, "rpm": 7000.0, "gear": 6, "angle": 0.02, "track_pos": 0.05,
         "track": corner, "opponents": clear_opponents},
        {"speed_x": 245.0, "rpm": 6800.0, "gear": 6, "angle": 0.0, "track_pos": -0.1,
         "track": cruise_track, "opponents": clear_opponents},
        {"speed_x": 80.0, "rpm": 5000.0, "gear": 3, "angle": 0.0, "track_pos": 0.0,
         "track": straight, "opponents": close_opponents},
    ]
    strategies = [ATTACK, NORMAL, DEFEND, SAVE_FUEL, PIT]
    for i in range(len(frames)):
        frames[i] = dict(frames[i], _strategy=strategies[i % len(strategies)])
    return frames


class ControlLoopComputeLatencyTests(unittest.TestCase):
    def setUp(self):
        ai_bot.set_track_model(None)
        ai_bot._reset_driver_state()

    def tearDown(self):
        ai_bot._reset_driver_state()

    def test_compute_control_1000_frame_latency_distribution(self):
        base_frames = _frame_states()
        n_frames = 1200
        samples: list[float] = []
        for i in range(n_frames):
            state = base_frames[i % len(base_frames)]
            strategy = state["_strategy"]
            call_state = {k: v for k, v in state.items() if k != "_strategy"}
            u0 = time.perf_counter()
            out = compute_control(call_state, strategy)
            u1 = time.perf_counter()
            self.assertIsInstance(out, str)
            samples.append(u1 - u0)

        self.assertEqual(len(samples), n_frames)
        median = statistics.median(samples)
        p95 = _percentile(samples, 0.95)
        maximum = max(samples)

        self.assertLess(
            median, _MEDIAN_BUDGET_S,
            f"compute_control median latency {median * 1000:.3f}ms exceeds "
            f"bot_test_plan.md 6.4's {_MEDIAN_BUDGET_S * 1000:.0f}ms target "
            f"(N={n_frames}, P95={p95 * 1000:.3f}ms, max={maximum * 1000:.3f}ms)",
        )
        self.assertLess(
            p95, _P95_BUDGET_S,
            f"compute_control P95 latency {p95 * 1000:.3f}ms exceeds "
            f"bot_test_plan.md 6.4's {_P95_BUDGET_S * 1000:.0f}ms target "
            f"(N={n_frames}, median={median * 1000:.3f}ms, max={maximum * 1000:.3f}ms)",
        )
