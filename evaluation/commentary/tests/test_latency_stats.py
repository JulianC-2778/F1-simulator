import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from latency_stats import Stats, percentile  # noqa: E402


def test_percentile_empty_raises():
    with pytest.raises(ValueError):
        percentile([], 0.95)


def test_percentile_invalid_p_raises():
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


def test_percentile_single_value_sample():
    assert percentile([42.0], 0.95) == 42.0
    assert percentile([42.0], 0.0) == 42.0
    assert percentile([42.0], 1.0) == 42.0


def test_percentile_p0_is_min_p1_is_max():
    values = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
    assert percentile(values, 0.0) == min(values)
    assert percentile(values, 1.0) == max(values)


def test_percentile_median_matches_p50_for_odd_count():
    values = [1.0, 3.0, 2.0]  # sorted: 1,2,3 -- median 2.0, rank=0.5*2=1.0
    assert percentile(values, 0.5) == 2.0


def test_percentile_known_linear_interpolation_value():
    # 0..100 in steps of 10 (n=11): rank = 0.95*10 = 9.5 -> between index 9
    # (90) and 10 (100) -> 90 + 0.5*(100-90) = 95.
    values = [float(i * 10) for i in range(11)]
    assert percentile(values, 0.95) == 95.0


def test_percentile_two_value_sample():
    # rank = 0.95 * 1 = 0.95 -> between 0 and 1 -> 10 + 0.95*(20-10) = 19.5
    assert percentile([10.0, 20.0], 0.95) == 19.5


def test_stats_from_empty_samples_reports_zero_count_and_none_fields():
    stats = Stats.from_samples([], failures=3)
    assert stats.count == 0
    assert stats.minimum is None and stats.mean is None and stats.p95 is None
    assert stats.failures == 3


def test_stats_from_single_sample_stdev_is_zero_not_an_error():
    stats = Stats.from_samples([5.0], failures=0)
    assert stats.count == 1
    assert stats.stdev == 0.0
    assert stats.median == 5.0
    assert stats.p95 == 5.0


def test_stats_row_formats_na_for_empty_and_numbers_for_normal():
    empty_row = Stats.from_samples([], failures=1).row("Event detection")
    assert empty_row == ["Event detection", "0", "N/A", "N/A", "N/A", "1"]

    normal_row = Stats.from_samples([1.0, 2.0, 3.0], failures=0).row("Event detection")
    assert normal_row[0] == "Event detection"
    assert normal_row[1] == "3"
    assert normal_row[5] == "0"
