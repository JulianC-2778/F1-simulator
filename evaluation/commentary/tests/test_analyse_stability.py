import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analyse_stability import (  # noqa: E402
    recovery_time_s,
    success_rate_pct,
    summarize_faults,
    violation_rate_pct,
)


def test_success_rate_normal_case():
    assert success_rate_pct(90, 100) == "90.00%"


def test_success_rate_zero_requests_is_n_a_not_zero_percent():
    assert success_rate_pct(0, 0) == "N/A"


def test_success_rate_can_exceed_100_if_data_is_inconsistent_and_is_not_clamped():
    # Not our job to silently "fix" bad input data -- report what's there.
    assert success_rate_pct(5, 4) == "125.00%"


def test_violation_rate_zero_total_is_n_a():
    assert violation_rate_pct(0, 0) == "N/A"
    assert violation_rate_pct(None, None) == "N/A"


def test_violation_rate_normal_case():
    assert violation_rate_pct(9, 131) == "6.87%"


def fault_row(**overrides):
    base = {
        "trial_id": "T01", "fault_id": "RT-01", "fault_injected_at_s": "60.0",
        "service_restored_at_s": "90.0", "first_success_after_restore_at_s": "91.4",
        "recovered": "true", "crashed": "false", "notes": "",
    }
    base.update(overrides)
    return base


def test_recovery_time_normal_case():
    assert abs(recovery_time_s(fault_row()) - 1.4) < 1e-9


def test_recovery_time_missing_restoration_is_none_not_error():
    # RT-07 (invalid telemetry) has no restoration step per
    # commentary_test_plan.md 8.2's results table (N/A column).
    row = fault_row(service_restored_at_s="", first_success_after_restore_at_s="")
    assert recovery_time_s(row) is None


def test_summarize_faults_all_recovered_no_crash_is_pass():
    summary = summarize_faults([fault_row(trial_id="T01"), fault_row(trial_id="T02")])
    assert summary["RT-01"]["trials"] == 2
    assert summary["RT-01"]["recovered"] == 2
    assert summary["RT-01"]["crashed"] == 0
    assert summary["RT-01"]["result"] == "PASS"
    assert abs(summary["RT-01"]["median_recovery_s"] - 1.4) < 1e-9


def test_summarize_faults_any_crash_is_fail_even_if_others_recovered():
    rows = [
        fault_row(trial_id="T01", crashed="false", recovered="true"),
        fault_row(trial_id="T02", crashed="true", recovered="false"),
    ]
    summary = summarize_faults(rows)
    assert summary["RT-01"]["result"] == "FAIL (crash in 1/2)"


def test_summarize_faults_partial_recovery_without_crash():
    rows = [
        fault_row(trial_id="T01", recovered="true"),
        fault_row(trial_id="T02", recovered="false", service_restored_at_s="", first_success_after_restore_at_s=""),
    ]
    summary = summarize_faults(rows)
    assert summary["RT-01"]["result"] == "PARTIAL (1/2 recovered)"


def test_summarize_faults_groups_by_fault_id_independently():
    rows = [
        fault_row(trial_id="T01", fault_id="RT-01"),
        fault_row(trial_id="T02", fault_id="RT-07", service_restored_at_s="", first_success_after_restore_at_s=""),
    ]
    summary = summarize_faults(rows)
    assert set(summary) == {"RT-01", "RT-07"}
    assert summary["RT-07"]["median_recovery_s"] is None
