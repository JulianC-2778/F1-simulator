import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analyse_latency import compute_stats  # noqa: E402


def row(**overrides):
    base = {
        "session_id": "S01", "event_id": "GT0001", "request_id": "REQ0001",
        "t0_telemetry_received": "10.000", "t1_event_detected": "10.300",
        "t2_first_token": "11.800", "t3_ai_done": "14.500",
        "t4_caption_displayed": "14.600", "t5_tts_started": "",
        "failed": "false", "failure_reason": "",
    }
    base.update(overrides)
    return base


def test_normal_row_produces_all_but_tts_sample():
    results = compute_stats([row()])
    assert results["Event detection"].count == 1
    assert abs(results["Event detection"].median - 0.3) < 1e-9
    assert results["First model token"].count == 1
    assert abs(results["First model token"].median - 1.5) < 1e-9
    assert results["Complete model response"].count == 1
    assert abs(results["Complete model response"].median - 4.2) < 1e-9
    assert results["Caption displayed"].count == 1
    # TTS disabled for this row (t5 empty) -- no sample, no failure either.
    assert results["TTS playback, if enabled"].count == 0
    assert results["TTS playback, if enabled"].failures == 0


def test_failed_row_counts_as_failure_for_every_stage_not_dropped():
    results = compute_stats([row(failed="true", failure_reason="granite_timeout")])
    for label in results:
        assert results[label].count == 0
        assert results[label].failures == 1


def test_missing_timestamp_counts_as_failure_for_that_stage_only():
    results = compute_stats([row(t3_ai_done="")])
    assert results["Complete model response"].count == 0
    assert results["Complete model response"].failures == 1
    # Event detection only needs t0/t1, both present -- must still count.
    assert results["Event detection"].count == 1
    assert results["Event detection"].failures == 0


def test_out_of_order_timestamps_count_as_failure_not_negative_duration():
    results = compute_stats([row(t1_event_detected="9.000")])  # before t0!
    assert results["Event detection"].count == 0
    assert results["Event detection"].failures == 1


def test_tts_sample_is_counted_when_present():
    results = compute_stats([row(t5_tts_started="15.000")])
    assert results["TTS playback, if enabled"].count == 1
    assert abs(results["TTS playback, if enabled"].median - 5.0) < 1e-9


def test_empty_row_list_produces_zero_counts_for_every_stage():
    results = compute_stats([])
    for label in results:
        assert results[label].count == 0
        assert results[label].failures == 0


def test_multiple_rows_aggregate_into_one_sample_per_stage():
    rows = [
        row(request_id="REQ0001", t0_telemetry_received="0.0", t1_event_detected="0.5"),
        row(request_id="REQ0002", t0_telemetry_received="10.0", t1_event_detected="10.3"),
        row(request_id="REQ0003", failed="true"),
    ]
    results = compute_stats(rows)
    assert results["Event detection"].count == 2
    assert results["Event detection"].failures == 1
