#!/usr/bin/env python3
"""Deterministic one-to-one matching of the bot's filtered strategy decisions
against human-annotated "expected strategy" readings, plus per-strategy-type
and overall (micro-averaged) Precision/Recall/F1 -- bot_test_plan.md 5.2-5.4.

Direct port of evaluation/commentary/scripts/match_events.py's matching
algorithm (see that file's docstring for the DP derivation and optimality
argument) to the strategy domain. The two problems have the same shape once
reframed as point-in-time readings instead of intervals:

  commentary event      -> [start_time_s, end_time_s] interval, event_type
  bot ground-truth read  -> a single timestamp_s, expected_strategy
  bot detected decision  -> a single timestamp_s, filtered_strategy

so each strategy reading here is modelled as a zero-width interval
(start_time_s == end_time_s == timestamp_s) and matched with exactly the
same "distance to interval, minimum-total-distance, no crossing pairs" DP,
just grouped by (session, strategy) instead of (session, event_type).
Tolerance default is 2.0s (bot_test_plan.md 5.2: wider than commentary's 1.0s
because a strategy is a state that can lag a moment, not an instantaneous
event) rather than commentary's 1.0s.

raw_granite_strategy is intentionally not part of this comparison -- work
package B measures whether safety_filter's *output* (filtered_strategy)
matches what should have happened, since that is what actually drives the
car. Comparing raw_granite_strategy separately (e.g. to gauge how often the
model's raw answer needed correcting) is a secondary analysis outside this
script's scope.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schemas"))
from csv_schemas import KNOWN_STRATEGIES  # noqa: E402


@dataclass(frozen=True)
class ExpectedReading:
    session: str
    reading_id: str
    expected_strategy: str
    timestamp_s: float

    @property
    def mid_time_s(self) -> float:
        return self.timestamp_s


@dataclass(frozen=True)
class DetectedDecision:
    session: str
    decision_id: str
    filtered_strategy: str
    timestamp_s: float


@dataclass(frozen=True)
class MatchResult:
    session: str
    strategy: str
    true_positives: list[tuple[ExpectedReading, DetectedDecision, float]]
    false_positives: list[DetectedDecision]
    false_negatives: list[ExpectedReading]


def load_ground_truth(path: Path) -> list[ExpectedReading]:
    with path.open(newline="", encoding="utf-8") as f:
        return [
            ExpectedReading(
                session=row["session"],
                reading_id=row["reading_id"],
                expected_strategy=row["expected_strategy"],
                timestamp_s=float(row["timestamp_s"]),
            )
            for row in csv.DictReader(f)
        ]


def load_detections(path: Path) -> list[DetectedDecision]:
    with path.open(newline="", encoding="utf-8") as f:
        return [
            DetectedDecision(
                session=row["session"],
                decision_id=row["decision_id"],
                filtered_strategy=row["filtered_strategy"],
                timestamp_s=float(row["timestamp_s"]),
            )
            for row in csv.DictReader(f)
        ]


def _distance(gt: ExpectedReading, det: DetectedDecision) -> float:
    """Both sides are point-in-time readings, so this is just |dt| -- the
    interval-distance shape is kept only for symmetry with match_events.py's
    _distance (a zero-width interval collapses to the same formula)."""
    return abs(det.timestamp_s - gt.timestamp_s)


def match_group(
    ground_truth: list[ExpectedReading],
    detections: list[DetectedDecision],
    tolerance: float,
) -> tuple[list[tuple[ExpectedReading, DetectedDecision, float]], list[DetectedDecision], list[ExpectedReading]]:
    gts = sorted(ground_truth, key=lambda e: (e.timestamp_s, e.reading_id))
    dets = sorted(detections, key=lambda e: (e.timestamp_s, e.decision_id))
    m, n = len(gts), len(dets)

    NEG_INF_MATCHES = -1

    dp = [[(NEG_INF_MATCHES, 0.0)] * (n + 1) for _ in range(m + 1)]
    choice = [[None] * (n + 1) for _ in range(m + 1)]
    dp[0][0] = (0, 0.0)

    def better(a: tuple[int, float], b: tuple[int, float]) -> bool:
        if a[0] != b[0]:
            return a[0] > b[0]
        return a[1] < b[1]

    for i in range(m + 1):
        for j in range(n + 1):
            if i == 0 and j == 0:
                continue
            best = (NEG_INF_MATCHES, 0.0)
            best_choice = None
            if i > 0 and dp[i - 1][j][0] > NEG_INF_MATCHES:
                candidate = dp[i - 1][j]
                if better(candidate, best):
                    best, best_choice = candidate, "skip_gt"
            if j > 0 and dp[i][j - 1][0] > NEG_INF_MATCHES:
                candidate = dp[i][j - 1]
                if better(candidate, best):
                    best, best_choice = candidate, "skip_det"
            if i > 0 and j > 0 and dp[i - 1][j - 1][0] > NEG_INF_MATCHES:
                dist = _distance(gts[i - 1], dets[j - 1])
                if dist <= tolerance:
                    prev = dp[i - 1][j - 1]
                    candidate = (prev[0] + 1, prev[1] + dist)
                    if better(candidate, best):
                        best, best_choice = candidate, "match"
            dp[i][j] = best
            choice[i][j] = best_choice

    matches: list[tuple[ExpectedReading, DetectedDecision, float]] = []
    matched_gt_ids: set[str] = set()
    matched_det_ids: set[str] = set()
    i, j = m, n
    while i > 0 or j > 0:
        c = choice[i][j]
        if c == "match":
            gt, det = gts[i - 1], dets[j - 1]
            matches.append((gt, det, _distance(gt, det)))
            matched_gt_ids.add(gt.reading_id)
            matched_det_ids.add(det.decision_id)
            i, j = i - 1, j - 1
        elif c == "skip_gt":
            i -= 1
        elif c == "skip_det":
            j -= 1
        else:  # pragma: no cover - only (0,0) has no choice
            break

    false_positives = [d for d in dets if d.decision_id not in matched_det_ids]
    false_negatives = [g for g in gts if g.reading_id not in matched_gt_ids]
    matches.sort(key=lambda triple: triple[0].timestamp_s)
    return matches, false_positives, false_negatives


def match_all(
    ground_truth: list[ExpectedReading],
    detections: list[DetectedDecision],
    tolerance: float,
) -> list[MatchResult]:
    keys = {(e.session, e.expected_strategy) for e in ground_truth} | {
        (e.session, e.filtered_strategy) for e in detections
    }
    results = []
    for session, strategy in sorted(keys):
        gt_group = [e for e in ground_truth if e.session == session and e.expected_strategy == strategy]
        det_group = [e for e in detections if e.session == session and e.filtered_strategy == strategy]
        tp, fp, fn = match_group(gt_group, det_group, tolerance)
        results.append(MatchResult(session, strategy, tp, fp, fn))
    return results


def precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[str, str, str]:
    precision = "N/A" if (tp + fp) == 0 else f"{tp / (tp + fp):.4f}"
    recall = "N/A" if (tp + fn) == 0 else f"{tp / (tp + fn):.4f}"
    if precision == "N/A" or recall == "N/A":
        f1 = "N/A"
    else:
        p, r = float(precision), float(recall)
        f1 = "N/A" if (p + r) == 0 else f"{2 * p * r / (p + r):.4f}"
    return precision, recall, f1


def summarize_by_strategy(results: list[MatchResult]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {t: {"tp": 0, "fp": 0, "fn": 0} for t in sorted(KNOWN_STRATEGIES)}
    for r in results:
        totals.setdefault(r.strategy, {"tp": 0, "fp": 0, "fn": 0})
        totals[r.strategy]["tp"] += len(r.true_positives)
        totals[r.strategy]["fp"] += len(r.false_positives)
        totals[r.strategy]["fn"] += len(r.false_negatives)
    return totals


def render_markdown_table(totals: dict[str, dict[str, int]]) -> str:
    lines = [
        "| Strategy | Ground truth | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    micro_tp = micro_fp = micro_fn = 0
    for strategy in sorted(totals):
        c = totals[strategy]
        gt_count = c["tp"] + c["fn"]
        p, r, f1 = precision_recall_f1(c["tp"], c["fp"], c["fn"])
        lines.append(f"| {strategy} | {gt_count} | {c['tp']} | {c['fp']} | {c['fn']} | {p} | {r} | {f1} |")
        micro_tp += c["tp"]
        micro_fp += c["fp"]
        micro_fn += c["fn"]
    p, r, f1 = precision_recall_f1(micro_tp, micro_fp, micro_fn)
    gt_total = micro_tp + micro_fn
    lines.append(f"| **Overall (micro)** | {gt_total} | {micro_tp} | {micro_fp} | {micro_fn} | {p} | {r} | {f1} |")
    return "\n".join(lines)


def write_detail_csvs(results: list[MatchResult], out_dir: Path, prefix: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{prefix}_true_positives.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session", "strategy", "reading_id", "decision_id", "gt_timestamp_s", "detected_timestamp_s", "distance_s"])
        for r in results:
            for gt, det, dist in r.true_positives:
                w.writerow([r.session, r.strategy, gt.reading_id, det.decision_id, gt.timestamp_s, det.timestamp_s, round(dist, 3)])
    with (out_dir / f"{prefix}_false_positives.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session", "strategy", "decision_id", "timestamp_s"])
        for r in results:
            for det in r.false_positives:
                w.writerow([r.session, r.strategy, det.decision_id, det.timestamp_s])
    with (out_dir / f"{prefix}_false_negatives.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session", "strategy", "reading_id", "timestamp_s"])
        for r in results:
            for gt in r.false_negatives:
                w.writerow([r.session, r.strategy, gt.reading_id, gt.timestamp_s])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--detections", required=True, type=Path)
    parser.add_argument("--tolerance", type=float, default=2.0, help="seconds, default 2.0")
    parser.add_argument("--out-dir", type=Path, default=None, help="if set, write TP/FP/FN detail CSVs here")
    parser.add_argument("--prefix", default="match", help="filename prefix for detail CSVs")
    args = parser.parse_args()

    ground_truth = load_ground_truth(args.ground_truth)
    detections = load_detections(args.detections)
    results = match_all(ground_truth, detections, args.tolerance)
    totals = summarize_by_strategy(results)
    print(render_markdown_table(totals))

    if args.out_dir:
        write_detail_csvs(results, args.out_dir, args.prefix)
        print(f"\nWrote detail CSVs to {args.out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
