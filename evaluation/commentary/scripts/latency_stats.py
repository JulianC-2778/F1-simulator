"""Shared descriptive-statistics helpers for latency and stability analysis.

P95 definition (documented once, used everywhere so results are
reproducible -- commentary_test_plan.md 7.3 requires this be pinned down,
not left to whatever a library defaults to): **linear interpolation on the
sorted sample**, i.e. numpy's default ``numpy.percentile(..., method="linear")``
/ Excel's ``PERCENTILE.INC``:

    sorted_values = sorted(values)
    rank = p * (n - 1)
    lower, upper = floor(rank), ceil(rank)
    result = sorted_values[lower] if lower == upper else
             sorted_values[lower] + (rank - lower) * (sorted_values[upper] - sorted_values[lower])

For n == 1, P95 is just that one value (rank is always 0).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


def percentile(values: list[float], p: float) -> float:
    if not values:
        raise ValueError("percentile() of an empty sample is undefined")
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must be in [0, 1], got {p}")
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    rank = p * (n - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (rank - lower) * (ordered[upper] - ordered[lower])


@dataclass(frozen=True)
class Stats:
    count: int
    minimum: float | None
    mean: float | None
    stdev: float | None
    median: float | None
    p95: float | None
    maximum: float | None
    failures: int

    @classmethod
    def from_samples(cls, values: list[float], failures: int) -> "Stats":
        if not values:
            return cls(0, None, None, None, None, None, None, failures)
        return cls(
            count=len(values),
            minimum=min(values),
            mean=statistics.fmean(values),
            stdev=statistics.stdev(values) if len(values) > 1 else 0.0,
            median=statistics.median(values),
            p95=percentile(values, 0.95),
            maximum=max(values),
            failures=failures,
        )

    def row(self, name: str) -> list[str]:
        def fmt(x):
            return "N/A" if x is None else f"{x:.3f}"

        return [name, str(self.count), fmt(self.median), fmt(self.p95), fmt(self.maximum), str(self.failures)]
