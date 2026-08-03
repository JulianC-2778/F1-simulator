#!/usr/bin/env python3
"""Deterministic one-to-one matching of detected events against
human-annotated ground truth, plus per-event-type and overall (micro-
averaged) Precision/Recall/F1 -- commentary_test_plan.md sections 6.3-6.4.

Matching algorithm
-------------------
Within each (session, event_type) group, ground-truth events and detections
are each sorted by time, then matched via a dynamic program that is
equivalent to a minimum-cost, maximum-cardinality bipartite matching
restricted to pairs within `tolerance` seconds of each other:

    dp[i][j] = best (matches, total_distance) achievable using the first i
               ground-truth events and the first j detections, comparing
               tuples lexicographically (more matches always wins; total
               distance is the tie-break).
    transitions from dp[i][j]:
      - leave ground_truth[i] unmatched (an eventual FN): dp[i+1][j]
      - leave detection[j] unmatched (an eventual FP):     dp[i][j+1]
      - match ground_truth[i] with detection[j], if
        |gt_time[i] - det_time[j]| <= tolerance:            dp[i+1][j+1]

This never lets two matched pairs "cross" in time, which is the standard
optimality property for minimum-total-distance matching of two point sets
on a line (the same structure as sequence-alignment DP) -- see
docs/commentary_test_matrix.md and evaluation/commentary/tests/test_match_events.py
for the specific edge cases (duplicate candidates, exact-tolerance boundary,
cross-session, wrong event type, empty input) this is tested against.
Both lists are sorted by time before matching, so unlike a greedy scan over
raw CSV row order, the result never depends on input row order.

pace_update is intentionally excluded (see schemas/csv_schemas.py
KNOWN_EVENT_TYPES) -- commentary_test_plan.md 6.1 checks it separately by
trigger interval, not as an F1 event.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schemas"))
from csv_schemas import KNOWN_EVENT_TYPES  # noqa: E402


@dataclass(frozen=True)
class GroundTruthEvent:
    session: str
    event_id: str
    event_type: str
    start_time_s: float
    end_time_s: float

    @property
    def mid_time_s(self) -> float:
        return (self.start_time_s + self.end_time_s) / 2.0


@dataclass(frozen=True)
class DetectedEvent:
    session: str
    detection_id: str
    event_type: str
    detection_time_s: float


@dataclass(frozen=True)
class MatchResult:
    session: str
    event_type: str
    true_positives: list[tuple[GroundTruthEvent, DetectedEvent, float]]
    false_positives: list[DetectedEvent]
    false_negatives: list[GroundTruthEvent]


def load_ground_truth(path: Path) -> list[GroundTruthEvent]:
    with path.open(newline="", encoding="utf-8") as f:
        return [
            GroundTruthEvent(
                session=row["session"],
                event_id=row["event_id"],
                event_type=row["event_type"],
                start_time_s=float(row["start_time_s"]),
                end_time_s=float(row["end_time_s"]),
            )
            for row in csv.DictReader(f)
        ]


def load_detections(path: Path) -> list[DetectedEvent]:
    with path.open(newline="", encoding="utf-8") as f:
        return [
            DetectedEvent(
                session=row["session"],
                detection_id=row["detection_id"],
                event_type=row["event_type"],
                detection_time_s=float(row["detection_time_s"]),
            )
            for row in csv.DictReader(f)
        ]


def _distance(gt: GroundTruthEvent, det: DetectedEvent) -> float:
    """Distance from detection_time_s to the *interval* [start, end], not to
    the midpoint -- a detection landing inside the annotated span has zero
    distance, matching commentary_test_plan.md 6.3's "distance to the
    human-annotated event interval" wording."""
    if det.detection_time_s < gt.start_time_s:
        return gt.start_time_s - det.detection_time_s
    if det.detection_time_s > gt.end_time_s:
        return det.detection_time_s - gt.end_time_s
    return 0.0


def match_group(
    ground_truth: list[GroundTruthEvent],
    detections: list[DetectedEvent],
    tolerance: float,
) -> tuple[list[tuple[GroundTruthEvent, DetectedEvent, float]], list[DetectedEvent], list[GroundTruthEvent]]:
    gts = sorted(ground_truth, key=lambda e: (e.start_time_s, e.event_id))
    dets = sorted(detections, key=lambda e: (e.detection_time_s, e.detection_id))
    m, n = len(gts), len(dets)

    NEG_INF_MATCHES = -1

    # dp[i][j] = (matches, total_distance); choice[i][j] in {"match","skip_gt","skip_det"}
    dp = [[(NEG_INF_MATCHES, 0.0)] * (n + 1) for _ in range(m + 1)]
    choice = [[None] * (n + 1) for _ in range(m + 1)]
    dp[0][0] = (0, 0.0)

    def better(a: tuple[int, float], b: tuple[int, float]) -> bool:
        """True if a is strictly better than b: more matches wins; ties
        broken by lower total distance."""
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

    # Backtrack.
    matches: list[tuple[GroundTruthEvent, DetectedEvent, float]] = []
    matched_gt_ids: set[str] = set()
    matched_det_ids: set[str] = set()
    i, j = m, n
    while i > 0 or j > 0:
        c = choice[i][j]
        if c == "match":
            gt, det = gts[i - 1], dets[j - 1]
            matches.append((gt, det, _distance(gt, det)))
            matched_gt_ids.add(gt.event_id)
            matched_det_ids.add(det.detection_id)
            i, j = i - 1, j - 1
        elif c == "skip_gt":
            i -= 1
        elif c == "skip_det":
            j -= 1
        else:  # pragma: no cover - only (0,0) has no choice
            break

    false_positives = [d for d in dets if d.detection_id not in matched_det_ids]
    false_negatives = [g for g in gts if g.event_id not in matched_gt_ids]
    matches.sort(key=lambda triple: triple[0].start_time_s)
    return matches, false_positives, false_negatives


def match_all(
    ground_truth: list[GroundTruthEvent],
    detections: list[DetectedEvent],
    tolerance: float,
) -> list[MatchResult]:
    keys = {(e.session, e.event_type) for e in ground_truth} | {(e.session, e.event_type) for e in detections}
    results = []
    for session, event_type in sorted(keys):
        gt_group = [e for e in ground_truth if e.session == session and e.event_type == event_type]
        det_group = [e for e in detections if e.session == session and e.event_type == event_type]
        tp, fp, fn = match_group(gt_group, det_group, tolerance)
        results.append(MatchResult(session, event_type, tp, fp, fn))
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


def summarize_by_event_type(results: list[MatchResult]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {t: {"tp": 0, "fp": 0, "fn": 0} for t in sorted(KNOWN_EVENT_TYPES)}
    for r in results:
        totals.setdefault(r.event_type, {"tp": 0, "fp": 0, "fn": 0})
        totals[r.event_type]["tp"] += len(r.true_positives)
        totals[r.event_type]["fp"] += len(r.false_positives)
        totals[r.event_type]["fn"] += len(r.false_negatives)
    return totals


def render_markdown_table(totals: dict[str, dict[str, int]]) -> str:
    lines = [
        "| Event | Ground truth | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    micro_tp = micro_fp = micro_fn = 0
    for event_type in sorted(totals):
        c = totals[event_type]
        gt_count = c["tp"] + c["fn"]
        p, r, f1 = precision_recall_f1(c["tp"], c["fp"], c["fn"])
        label = event_type.replace("_", " ").title()
        lines.append(f"| {label} | {gt_count} | {c['tp']} | {c['fp']} | {c['fn']} | {p} | {r} | {f1} |")
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
        w.writerow(["session", "event_type", "event_id", "detection_id", "gt_start_time_s", "gt_end_time_s", "detection_time_s", "distance_s"])
        for r in results:
            for gt, det, dist in r.true_positives:
                w.writerow([r.session, r.event_type, gt.event_id, det.detection_id, gt.start_time_s, gt.end_time_s, det.detection_time_s, round(dist, 3)])
    with (out_dir / f"{prefix}_false_positives.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session", "event_type", "detection_id", "detection_time_s"])
        for r in results:
            for det in r.false_positives:
                w.writerow([r.session, r.event_type, det.detection_id, det.detection_time_s])
    with (out_dir / f"{prefix}_false_negatives.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session", "event_type", "event_id", "start_time_s", "end_time_s"])
        for r in results:
            for gt in r.false_negatives:
                w.writerow([r.session, r.event_type, gt.event_id, gt.start_time_s, gt.end_time_s])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--detections", required=True, type=Path)
    parser.add_argument("--tolerance", type=float, default=1.0, help="seconds, default 1.0")
    parser.add_argument("--out-dir", type=Path, default=None, help="if set, write TP/FP/FN detail CSVs here")
    parser.add_argument("--prefix", default="match", help="filename prefix for detail CSVs")
    args = parser.parse_args()

    ground_truth = load_ground_truth(args.ground_truth)
    detections = load_detections(args.detections)
    results = match_all(ground_truth, detections, args.tolerance)
    totals = summarize_by_event_type(results)
    print(render_markdown_table(totals))

    if args.out_dir:
        write_detail_csvs(results, args.out_dir, args.prefix)
        print(f"\nWrote detail CSVs to {args.out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
