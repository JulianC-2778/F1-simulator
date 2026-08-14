import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analyse_control_latency import compute_stats  # noqa: E402


def frame_row(**overrides):
    base = {
        "session": "SC1", "record_id": "F00001", "kind": "frame",
        "u0_scr_state_received": "1000.000000", "u1_control_computed": "1000.001800",
        "u2_control_sent": "1000.002100",
        "g0_state_snapshot": "", "g1_first_byte": "", "g2_response_complete": "", "g3_strategy_applied": "",
        "failed": "false", "failure_reason": "",
    }
    base.update(overrides)
    return base


def granite_row(**overrides):
    base = {
        "session": "SC1", "record_id": "G00001", "kind": "granite_rtt",
        "u0_scr_state_received": "", "u1_control_computed": "", "u2_control_sent": "",
        "g0_state_snapshot": "1000.000000", "g1_first_byte": "",
        "g2_response_complete": "1002.150000", "g3_strategy_applied": "1002.151000",
        "failed": "false", "failure_reason": "",
    }
    base.update(overrides)
    return base


def test_normal_frame_row_produces_control_loop_samples_only():
    results = compute_stats([frame_row()])
    assert results["Control loop (compute)"].count == 1
    assert abs(results["Control loop (compute)"].median - 0.0018) < 1e-9
    assert results["Control loop (send, UDP)"].count == 1
    assert abs(results["Control loop (send, UDP)"].median - 0.0003) < 1e-9
    assert results["Control loop (frame, total)"].count == 1
    assert abs(results["Control loop (frame, total)"].median - 0.0021) < 1e-9
    # A frame row must never contribute a Granite sample or failure -- it
    # simply doesn't carry that chain's columns.
    assert results["Granite strategy RTT"].count == 0
    assert results["Granite strategy RTT"].failures == 0


def test_normal_granite_row_produces_granite_samples_only():
    results = compute_stats([granite_row()])
    assert results["Granite strategy RTT"].count == 1
    assert abs(results["Granite strategy RTT"].median - 2.15) < 1e-9
    assert results["Granite debounce overhead"].count == 1
    assert abs(results["Granite debounce overhead"].median - 0.001) < 1e-9
    assert results["Control loop (compute)"].count == 0
    assert results["Control loop (compute)"].failures == 0


def test_failed_frame_row_counts_as_failure_for_control_loop_metrics_only():
    results = compute_stats([frame_row(failed="true", failure_reason="control_exception")])
    for label in ("Control loop (compute)", "Control loop (send, UDP)", "Control loop (frame, total)"):
        assert results[label].count == 0
        assert results[label].failures == 1
    # Must NOT leak into the unrelated Granite chain just because the row
    # happens to be marked failed -- a frame failure isn't a Granite failure.
    for label in ("Granite strategy RTT", "Granite debounce overhead"):
        assert results[label].failures == 0


def test_failed_granite_row_counts_as_failure_for_granite_metrics_only():
    results = compute_stats([granite_row(failed="true", failure_reason="granite_timeout")])
    for label in ("Granite strategy RTT", "Granite debounce overhead"):
        assert results[label].count == 0
        assert results[label].failures == 1
    for label in ("Control loop (compute)", "Control loop (send, UDP)", "Control loop (frame, total)"):
        assert results[label].failures == 0


def test_missing_timestamp_on_a_claimed_kind_counts_as_failure():
    results = compute_stats([frame_row(u1_control_computed="")])
    assert results["Control loop (compute)"].count == 0
    assert results["Control loop (compute)"].failures == 1


def test_out_of_order_timestamps_count_as_failure_not_negative_duration():
    results = compute_stats([frame_row(u1_control_computed="999.000000")])  # before u0!
    assert results["Control loop (compute)"].count == 0
    assert results["Control loop (compute)"].failures == 1


def test_empty_row_list_produces_zero_counts_for_every_stage():
    results = compute_stats([])
    for label in results:
        assert results[label].count == 0
        assert results[label].failures == 0


def test_multiple_rows_of_both_kinds_aggregate_independently():
    rows = [
        frame_row(record_id="F1"),
        frame_row(record_id="F2", u0_scr_state_received="2000.0", u1_control_computed="2000.0015", u2_control_sent="2000.0018"),
        granite_row(record_id="G1"),
        granite_row(record_id="G2", failed="true"),
    ]
    results = compute_stats(rows)
    assert results["Control loop (compute)"].count == 2
    assert results["Control loop (compute)"].failures == 0
    assert results["Granite strategy RTT"].count == 1
    assert results["Granite strategy RTT"].failures == 1
