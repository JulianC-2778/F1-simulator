import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from match_events import (  # noqa: E402
    DetectedEvent,
    GroundTruthEvent,
    match_all,
    match_group,
    precision_recall_f1,
    summarize_by_event_type,
)


def gt(event_id, session="S01", event_type="contact", start=10.0, end=10.5):
    return GroundTruthEvent(session, event_id, event_type, start, end)


def det(detection_id, session="S01", event_type="contact", time=10.2):
    return DetectedEvent(session, detection_id, event_type, time)


def test_empty_input_produces_no_matches_no_errors():
    tp, fp, fn = match_group([], [], tolerance=1.0)
    assert tp == [] and fp == [] and fn == []


def test_empty_ground_truth_all_detections_are_false_positives():
    tp, fp, fn = match_group([], [det("D1")], tolerance=1.0)
    assert tp == []
    assert [d.detection_id for d in fp] == ["D1"]
    assert fn == []


def test_empty_detections_all_ground_truth_are_false_negatives():
    tp, fp, fn = match_group([gt("G1")], [], tolerance=1.0)
    assert tp == []
    assert fp == []
    assert [g.event_id for g in fn] == ["G1"]


def test_within_interval_has_zero_distance_and_matches():
    tp, fp, fn = match_group([gt("G1", start=10.0, end=10.5)], [det("D1", time=10.2)], tolerance=1.0)
    assert len(tp) == 1
    assert tp[0][2] == 0.0


def test_exactly_at_tolerance_boundary_matches_inclusive():
    # detection is exactly 1.0s after the interval end -- distance == tolerance.
    tp, fp, fn = match_group(
        [gt("G1", start=10.0, end=10.0)], [det("D1", time=11.0)], tolerance=1.0,
    )
    assert len(tp) == 1
    assert len(fp) == 0 and len(fn) == 0


def test_just_over_tolerance_boundary_does_not_match():
    tp, fp, fn = match_group(
        [gt("G1", start=10.0, end=10.0)], [det("D1", time=11.001)], tolerance=1.0,
    )
    assert len(tp) == 0
    assert [d.detection_id for d in fp] == ["D1"]
    assert [g.event_id for g in fn] == ["G1"]


def test_wrong_event_type_never_matches_even_if_close_in_time():
    # match_group is called per (session, event_type) group by match_all --
    # this test proves that grouping, not just a coincidental time gap.
    ground_truth = [gt("G1", event_type="contact", start=10.0, end=10.0)]
    detections = [det("D1", event_type="off_track", time=10.1)]
    results = match_all(ground_truth, detections, tolerance=1.0)
    contact_result = next(r for r in results if r.event_type == "contact")
    off_track_result = next(r for r in results if r.event_type == "off_track")
    assert contact_result.true_positives == []
    assert [g.event_id for g in contact_result.false_negatives] == ["G1"]
    assert off_track_result.true_positives == []
    assert [d.detection_id for d in off_track_result.false_positives] == ["D1"]


def test_cross_session_never_matches_even_if_close_in_time():
    ground_truth = [gt("G1", session="S01", start=10.0, end=10.0)]
    detections = [det("D1", session="S02", time=10.1)]
    results = match_all(ground_truth, detections, tolerance=1.0)
    s01 = next(r for r in results if r.session == "S01")
    s02 = next(r for r in results if r.session == "S02")
    assert s01.true_positives == [] and [g.event_id for g in s01.false_negatives] == ["G1"]
    assert s02.true_positives == [] and [d.detection_id for d in s02.false_positives] == ["D1"]


def test_duplicate_candidates_each_ground_truth_matches_at_most_once():
    # Two ground-truth events close together, two detections close together
    # -- must not let one detection match two ground truths or vice versa.
    ground_truth = [gt("G1", start=10.0, end=10.0), gt("G2", start=10.3, end=10.3)]
    detections = [det("D1", time=10.05), det("D2", time=10.25)]
    tp, fp, fn = match_group(ground_truth, detections, tolerance=1.0)
    assert len(tp) == 2
    assert fp == [] and fn == []
    matched_gt_ids = {m[0].event_id for m in tp}
    matched_det_ids = {m[1].detection_id for m in tp}
    assert matched_gt_ids == {"G1", "G2"}
    assert matched_det_ids == {"D1", "D2"}


def test_single_detection_near_two_ground_truths_matches_only_the_closer_one():
    ground_truth = [gt("G1", start=10.0, end=10.0), gt("G2", start=10.8, end=10.8)]
    detections = [det("D1", time=10.7)]
    tp, fp, fn = match_group(ground_truth, detections, tolerance=1.0)
    assert len(tp) == 1
    assert tp[0][0].event_id == "G2"  # 0.1s away, closer than G1's 0.7s
    assert [g.event_id for g in fn] == ["G1"]


def test_result_is_independent_of_input_row_order():
    ground_truth = [gt("G1", start=10.0, end=10.0), gt("G2", start=20.0, end=20.0)]
    detections = [det("D1", time=20.1), det("D2", time=10.1)]  # deliberately out of order
    tp_a, fp_a, fn_a = match_group(ground_truth, detections, tolerance=1.0)
    tp_b, fp_b, fn_b = match_group(list(reversed(ground_truth)), list(reversed(detections)), tolerance=1.0)
    pairs_a = sorted((m[0].event_id, m[1].detection_id) for m in tp_a)
    pairs_b = sorted((m[0].event_id, m[1].detection_id) for m in tp_b)
    assert pairs_a == pairs_b == [("G1", "D2"), ("G2", "D1")]


def test_precision_recall_f1_zero_division_reports_n_a():
    assert precision_recall_f1(0, 0, 0) == ("N/A", "N/A", "N/A")
    p, r, f1 = precision_recall_f1(0, 0, 5)
    assert p == "N/A"  # tp+fp == 0
    assert r == "0.0000"


def test_precision_recall_f1_normal_values():
    p, r, f1 = precision_recall_f1(tp=8, fp=2, fn=2)
    assert p == "0.8000"
    assert r == "0.8000"
    assert f1 == "0.8000"


def test_overall_micro_average_sums_counts_not_averages_per_type_f1():
    # Two event types with very different sample sizes -- micro average must
    # weight by count, not just average the two F1 scores.
    ground_truth = [gt(f"G{i}", event_type="contact", start=float(i), end=float(i)) for i in range(10)]
    detections = [det(f"D{i}", event_type="contact", time=float(i)) for i in range(8)]  # 8 TP, 2 FN
    ground_truth += [gt("GB1", event_type="battle", start=100.0, end=100.0)]
    detections += [det("DB1", event_type="battle", time=100.0), det("DB2", event_type="battle", time=200.0)]  # 1 TP, 1 FP
    results = match_all(ground_truth, detections, tolerance=1.0)
    totals = summarize_by_event_type(results)
    micro_tp = sum(c["tp"] for c in totals.values())
    micro_fp = sum(c["fp"] for c in totals.values())
    micro_fn = sum(c["fn"] for c in totals.values())
    assert micro_tp == 9
    assert micro_fp == 1
    assert micro_fn == 2
    p, r, f1 = precision_recall_f1(micro_tp, micro_fp, micro_fn)
    assert p == "0.9000"
    assert r == "0.8182"
