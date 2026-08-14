import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from match_strategy_decisions import (  # noqa: E402
    DetectedDecision,
    ExpectedReading,
    match_all,
    match_group,
    precision_recall_f1,
    summarize_by_strategy,
)


def gt(reading_id, session="SA1", strategy="NORMAL", t=42.0):
    return ExpectedReading(session, reading_id, strategy, t)


def det(decision_id, session="SA1", strategy="NORMAL", t=42.2):
    return DetectedDecision(session, decision_id, strategy, t)


def test_empty_input_produces_no_matches_no_errors():
    tp, fp, fn = match_group([], [], tolerance=2.0)
    assert tp == [] and fp == [] and fn == []


def test_empty_ground_truth_all_detections_are_false_positives():
    tp, fp, fn = match_group([], [det("D1")], tolerance=2.0)
    assert tp == []
    assert [d.decision_id for d in fp] == ["D1"]
    assert fn == []


def test_empty_detections_all_ground_truth_are_false_negatives():
    tp, fp, fn = match_group([gt("G1")], [], tolerance=2.0)
    assert tp == []
    assert fp == []
    assert [g.reading_id for g in fn] == ["G1"]


def test_within_tolerance_matches():
    tp, fp, fn = match_group([gt("G1", t=42.0)], [det("D1", t=43.5)], tolerance=2.0)
    assert len(tp) == 1
    assert abs(tp[0][2] - 1.5) < 1e-9


def test_exactly_at_tolerance_boundary_matches_inclusive():
    tp, fp, fn = match_group([gt("G1", t=10.0)], [det("D1", t=12.0)], tolerance=2.0)
    assert len(tp) == 1
    assert len(fp) == 0 and len(fn) == 0


def test_just_over_tolerance_boundary_does_not_match():
    tp, fp, fn = match_group([gt("G1", t=10.0)], [det("D1", t=12.001)], tolerance=2.0)
    assert len(tp) == 0
    assert [d.decision_id for d in fp] == ["D1"]
    assert [g.reading_id for g in fn] == ["G1"]


def test_wrong_strategy_never_matches_even_if_close_in_time():
    # match_group is called per (session, strategy) group by match_all --
    # this test proves that grouping, not just a coincidental time gap.
    ground_truth = [gt("G1", strategy="PIT", t=10.0)]
    detections = [det("D1", strategy="NORMAL", t=10.1)]
    results = match_all(ground_truth, detections, tolerance=2.0)
    pit_result = next(r for r in results if r.strategy == "PIT")
    normal_result = next(r for r in results if r.strategy == "NORMAL")
    assert pit_result.true_positives == []
    assert [g.reading_id for g in pit_result.false_negatives] == ["G1"]
    assert normal_result.true_positives == []
    assert [d.decision_id for d in normal_result.false_positives] == ["D1"]


def test_block_being_the_expected_strategy_matches_a_block_detection():
    # BLOCK is system-only in ai_bot.py (never reachable from Granite text)
    # but IS matchable here: expected_strategy=BLOCK, filtered_strategy=BLOCK
    # is exactly the "safety_filter correctly triggered the rear-gap rule"
    # case work package B is supposed to score as a true positive.
    ground_truth = [gt("G1", strategy="BLOCK", t=120.0)]
    detections = [det("D1", strategy="BLOCK", t=121.0)]
    results = match_all(ground_truth, detections, tolerance=2.0)
    block_result = next(r for r in results if r.strategy == "BLOCK")
    assert len(block_result.true_positives) == 1


def test_cross_session_never_matches_even_if_close_in_time():
    ground_truth = [gt("G1", session="SA1", t=10.0)]
    detections = [det("D1", session="SA2", t=10.1)]
    results = match_all(ground_truth, detections, tolerance=2.0)
    sa1 = next(r for r in results if r.session == "SA1")
    sa2 = next(r for r in results if r.session == "SA2")
    assert sa1.true_positives == [] and [g.reading_id for g in sa1.false_negatives] == ["G1"]
    assert sa2.true_positives == [] and [d.decision_id for d in sa2.false_positives] == ["D1"]


def test_duplicate_candidates_each_ground_truth_matches_at_most_once():
    ground_truth = [gt("G1", t=10.0), gt("G2", t=10.3)]
    detections = [det("D1", t=10.05), det("D2", t=10.25)]
    tp, fp, fn = match_group(ground_truth, detections, tolerance=2.0)
    assert len(tp) == 2
    assert fp == [] and fn == []
    matched_gt_ids = {m[0].reading_id for m in tp}
    matched_det_ids = {m[1].decision_id for m in tp}
    assert matched_gt_ids == {"G1", "G2"}
    assert matched_det_ids == {"D1", "D2"}


def test_single_detection_near_two_ground_truths_matches_only_the_closer_one():
    ground_truth = [gt("G1", t=10.0), gt("G2", t=10.8)]
    detections = [det("D1", t=10.7)]
    tp, fp, fn = match_group(ground_truth, detections, tolerance=2.0)
    assert len(tp) == 1
    assert tp[0][0].reading_id == "G2"  # 0.1s away, closer than G1's 0.7s
    assert [g.reading_id for g in fn] == ["G1"]


def test_result_is_independent_of_input_row_order():
    ground_truth = [gt("G1", t=10.0), gt("G2", t=20.0)]
    detections = [det("D1", t=20.1), det("D2", t=10.1)]  # deliberately out of order
    tp_a, fp_a, fn_a = match_group(ground_truth, detections, tolerance=2.0)
    tp_b, fp_b, fn_b = match_group(list(reversed(ground_truth)), list(reversed(detections)), tolerance=2.0)
    pairs_a = sorted((m[0].reading_id, m[1].decision_id) for m in tp_a)
    pairs_b = sorted((m[0].reading_id, m[1].decision_id) for m in tp_b)
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


def test_overall_micro_average_sums_counts_not_averages_per_strategy_f1():
    # PIT (safety-critical, small N) and NORMAL (large N) must be weighted
    # by count in the micro average, not averaged as two equal F1 scores --
    # bot_test_plan.md 5.4 explicitly holds PIT/DEFEND recall to a higher bar
    # than the overall figure, which only makes sense if overall is
    # count-weighted.
    ground_truth = [gt(f"G{i}", strategy="NORMAL", t=float(i)) for i in range(10)]
    detections = [det(f"D{i}", strategy="NORMAL", t=float(i)) for i in range(8)]  # 8 TP, 2 FN
    ground_truth += [gt("GP1", strategy="PIT", t=100.0)]
    detections += [det("DP1", strategy="PIT", t=100.0), det("DP2", strategy="PIT", t=200.0)]  # 1 TP, 1 FP
    results = match_all(ground_truth, detections, tolerance=2.0)
    totals = summarize_by_strategy(results)
    micro_tp = sum(c["tp"] for c in totals.values())
    micro_fp = sum(c["fp"] for c in totals.values())
    micro_fn = sum(c["fn"] for c in totals.values())
    assert micro_tp == 9
    assert micro_fp == 1
    assert micro_fn == 2
    p, r, f1 = precision_recall_f1(micro_tp, micro_fp, micro_fn)
    assert p == "0.9000"
    assert r == "0.8182"
