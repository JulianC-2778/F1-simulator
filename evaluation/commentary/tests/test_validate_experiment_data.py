import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schemas"))

from csv_schemas import GROUND_TRUTH_SCHEMA, DETECTED_EVENTS_SCHEMA  # noqa: E402
from validate_experiment_data import validate_rows  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def gt_row(**overrides):
    row = {
        "session": "S01",
        "event_id": "GT0001",
        "event_type": "contact",
        "start_time_s": "10.0",
        "end_time_s": "10.5",
        "description": "test",
        "annotator": "A1",
    }
    row.update(overrides)
    return row


def test_valid_row_produces_no_errors():
    errors = validate_rows([gt_row()], GROUND_TRUTH_SCHEMA)
    assert errors == []


def test_empty_dataset_produces_clear_message():
    errors = validate_rows([], GROUND_TRUTH_SCHEMA)
    assert len(errors) == 1
    assert "zero data rows" in errors[0]


def test_missing_required_column_is_reported():
    row = gt_row()
    del row["event_type"]
    errors = validate_rows([row], GROUND_TRUTH_SCHEMA)
    assert any("event_type" in e for e in errors)


def test_negative_time_is_rejected():
    errors = validate_rows([gt_row(start_time_s="-1.0")], GROUND_TRUTH_SCHEMA)
    assert any("start_time_s" in e and ">= 0" in e for e in errors)


def test_end_before_start_is_rejected():
    errors = validate_rows([gt_row(start_time_s="10.0", end_time_s="9.0")], GROUND_TRUTH_SCHEMA)
    assert any("must be >=" in e for e in errors)


def test_end_equals_start_is_allowed():
    errors = validate_rows([gt_row(start_time_s="10.0", end_time_s="10.0")], GROUND_TRUTH_SCHEMA)
    assert errors == []


def test_unrecognised_event_type_is_rejected():
    errors = validate_rows([gt_row(event_type="tire_smoke")], GROUND_TRUTH_SCHEMA)
    assert any("unrecognised event_type" in e for e in errors)


def test_pace_update_is_not_a_recognised_ground_truth_event_type():
    # commentary_test_plan.md 6.1: pace_update is checked separately for
    # trigger interval and explicitly excluded from event F1.
    errors = validate_rows([gt_row(event_type="pace_update")], GROUND_TRUTH_SCHEMA)
    assert any("unrecognised event_type" in e for e in errors)


def test_duplicate_id_within_file_is_rejected():
    rows = [gt_row(event_id="GT0001"), gt_row(event_id="GT0001", start_time_s="20.0", end_time_s="20.5")]
    errors = validate_rows(rows, GROUND_TRUTH_SCHEMA)
    assert any("duplicate event_id" in e for e in errors)


def test_non_numeric_time_field_is_rejected():
    errors = validate_rows([gt_row(start_time_s="not-a-number")], GROUND_TRUTH_SCHEMA)
    assert any("not a valid float" in e for e in errors)


def test_empty_session_is_rejected():
    errors = validate_rows([gt_row(session="")], GROUND_TRUTH_SCHEMA)
    assert any("session" in e and "empty" in e for e in errors)


def test_detected_events_priority_is_optional_but_type_checked_when_present():
    row = {
        "session": "S01", "detection_id": "DET0001", "event_type": "battle",
        "detection_time_s": "1.0", "priority": "not-an-int", "source": "commentary_engine",
    }
    errors = validate_rows([row], DETECTED_EVENTS_SCHEMA)
    assert any("priority" in e and "not a valid int" in e for e in errors)


def test_all_five_templates_are_valid():
    from csv_schemas import SCHEMAS
    import csv as csv_module

    template_files = {
        "ground_truth": "ground_truth_template.csv",
        "detected_events": "detected_events_template.csv",
        "latency": "latency_template.csv",
        "stability_run": "stability_run_template.csv",
        "fault_recovery": "fault_recovery_template.csv",
    }
    for kind, filename in template_files.items():
        path = TEMPLATES / filename
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv_module.DictReader(f))
        errors = validate_rows(rows, SCHEMAS[kind])
        assert errors == [], f"{filename} should be valid, got: {errors}"
