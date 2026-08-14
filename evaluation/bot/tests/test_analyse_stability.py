import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analyse_stability import (  # noqa: E402
    recovery_time_s,
    render_safety_filter_breakdown_table,
    success_rate_pct,
    summarize_faults,
)


def test_success_rate_normal_case():
    # 80 requests, 2 Granite failures -> 78 succeeded.
    assert success_rate_pct(80, 2) == "97.50%"


def test_success_rate_zero_requests_is_n_a_not_zero_percent():
    # A rules-only (Granite disabled) run makes zero strategy requests --
    # that must read N/A, not a misleading 0%.
    assert success_rate_pct(0, 0) == "N/A"


def test_success_rate_all_failed():
    assert success_rate_pct(10, 10) == "0.00%"


def fault_row(**overrides):
    base = {
        "trial_id": "T01", "fault_id": "RB-01", "fault_injected_at_s": "120.0",
        "service_restored_at_s": "150.0", "first_success_after_restore_at_s": "155.2",
        "recovered": "true", "crashed": "false", "notes": "",
    }
    base.update(overrides)
    return base


def test_recovery_time_normal_case():
    assert abs(recovery_time_s(fault_row()) - 5.2) < 1e-9


def test_recovery_time_missing_restoration_is_none_not_error():
    # RB-05 (TORCS/scr_server disconnect) has no restoration step -- run_bot
    # exits cleanly instead, bot_test_plan.md 7.2's RB-05 row.
    row = fault_row(service_restored_at_s="", first_success_after_restore_at_s="")
    assert recovery_time_s(row) is None


def test_summarize_faults_all_recovered_no_crash_is_pass():
    summary = summarize_faults([fault_row(trial_id="T01"), fault_row(trial_id="T02")])
    assert summary["RB-01"]["trials"] == 2
    assert summary["RB-01"]["recovered"] == 2
    assert summary["RB-01"]["crashed"] == 0
    assert summary["RB-01"]["result"] == "PASS"
    assert abs(summary["RB-01"]["median_recovery_s"] - 5.2) < 1e-9


def test_summarize_faults_any_crash_is_fail_even_if_others_recovered():
    rows = [
        fault_row(trial_id="T01", crashed="false", recovered="true"),
        fault_row(trial_id="T02", crashed="true", recovered="false"),
    ]
    summary = summarize_faults(rows)
    assert summary["RB-01"]["result"] == "FAIL (crash in 1/2)"


def test_summarize_faults_partial_recovery_without_crash():
    rows = [
        fault_row(trial_id="T01", recovered="true"),
        fault_row(trial_id="T02", recovered="false", service_restored_at_s="", first_success_after_restore_at_s=""),
    ]
    summary = summarize_faults(rows)
    assert summary["RB-01"]["result"] == "PARTIAL (1/2 recovered)"


def test_summarize_faults_groups_by_fault_id_independently():
    rows = [
        fault_row(trial_id="T01", fault_id="RB-01"),
        fault_row(trial_id="T02", fault_id="RB-05", service_restored_at_s="", first_success_after_restore_at_s=""),
    ]
    summary = summarize_faults(rows)
    assert set(summary) == {"RB-01", "RB-05"}
    assert summary["RB-05"]["median_recovery_s"] is None


def stability_row(**overrides):
    base = {
        "run_id": "RB01", "duration_s": "1200.0",
        "safety_filter_pit_count": "1", "safety_filter_defend_count": "0",
        "safety_filter_block_count": "4", "safety_filter_normal_cap_count": "1",
    }
    base.update(overrides)
    return base


def test_safety_filter_breakdown_table_totals_per_type():
    table = render_safety_filter_breakdown_table([stability_row()])
    assert "| RB01 | 1 | 0 | 4 | 1 |" in table
    assert "| **Total** | 1 | 0 | 4 | 1 |" in table


def test_safety_filter_breakdown_table_missing_columns_reports_not_present():
    rows = [{"run_id": "RB01"}]
    table = render_safety_filter_breakdown_table(rows)
    assert "No per-type safety_filter breakdown columns present" in table
