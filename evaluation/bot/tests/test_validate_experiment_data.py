import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schemas"))

from csv_schemas import GROUND_TRUTH_STRATEGY_SCHEMA, DETECTED_STRATEGY_SCHEMA  # noqa: E402
from validate_experiment_data import validate_rows  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def gt_row(**overrides):
    row = {
        "session": "SA1",
        "reading_id": "GT0001",
        "timestamp_s": "42.3",
        "fuel_L": "12.1",
        "damage": "0",
        "rear_gap_m": "35.0",
        "expected_strategy": "NORMAL",
        "annotator": "A1",
    }
    row.update(overrides)
    return row


def test_valid_row_produces_no_errors():
    errors = validate_rows([gt_row()], GROUND_TRUTH_STRATEGY_SCHEMA)
    assert errors == []


def test_empty_dataset_produces_clear_message():
    errors = validate_rows([], GROUND_TRUTH_STRATEGY_SCHEMA)
    assert len(errors) == 1
    assert "zero data rows" in errors[0]


def test_missing_required_column_is_reported():
    row = gt_row()
    del row["expected_strategy"]
    errors = validate_rows([row], GROUND_TRUTH_STRATEGY_SCHEMA)
    assert any("expected_strategy" in e for e in errors)


def test_negative_fuel_is_rejected():
    errors = validate_rows([gt_row(fuel_L="-1.0")], GROUND_TRUTH_STRATEGY_SCHEMA)
    assert any("fuel_L" in e and ">= 0" in e for e in errors)


def test_unrecognised_strategy_is_rejected():
    errors = validate_rows([gt_row(expected_strategy="TURBO")], GROUND_TRUTH_STRATEGY_SCHEMA)
    assert any("unrecognised strategy" in e for e in errors)


def test_block_is_a_recognised_strategy_for_ground_truth():
    # BLOCK is system-only (never reachable from Granite's own text output --
    # ai_bot.py L1980/L2027/L2119) but it IS a valid *expected* strategy: an
    # annotator can correctly say "the safety net should have triggered
    # BLOCK here", and safety_filter is exactly what's expected to produce it.
    errors = validate_rows([gt_row(expected_strategy="BLOCK")], GROUND_TRUTH_STRATEGY_SCHEMA)
    assert errors == []


def test_duplicate_id_within_file_is_rejected():
    rows = [gt_row(reading_id="GT0001"), gt_row(reading_id="GT0001", timestamp_s="99.0")]
    errors = validate_rows(rows, GROUND_TRUTH_STRATEGY_SCHEMA)
    assert any("duplicate reading_id" in e for e in errors)


def test_non_numeric_time_field_is_rejected():
    errors = validate_rows([gt_row(timestamp_s="not-a-number")], GROUND_TRUTH_STRATEGY_SCHEMA)
    assert any("not a valid float" in e for e in errors)


def test_empty_session_is_rejected():
    errors = validate_rows([gt_row(session="")], GROUND_TRUTH_STRATEGY_SCHEMA)
    assert any("session" in e and "empty" in e for e in errors)


def test_raw_granite_strategy_is_not_vocabulary_checked():
    # Deliberately unrestricted -- proving the parse layer rejects
    # hallucinated raw text is the point of work package B, not something
    # this schema should pre-filter away. filtered_strategy IS checked.
    row = {
        "session": "SA1", "decision_id": "DEC0001", "timestamp_s": "10.0",
        "raw_granite_strategy": "TURBO_NITRO_BOOST", "filtered_strategy": "NORMAL",
        "source": "safety_filter",
    }
    errors = validate_rows([row], DETECTED_STRATEGY_SCHEMA)
    assert errors == []


def test_unrecognised_filtered_strategy_is_rejected():
    row = {
        "session": "SA1", "decision_id": "DEC0001", "timestamp_s": "10.0",
        "raw_granite_strategy": "NORMAL", "filtered_strategy": "TURBO",
        "source": "safety_filter",
    }
    errors = validate_rows([row], DETECTED_STRATEGY_SCHEMA)
    assert any("unrecognised strategy" in e and "filtered_strategy" in e for e in errors)


def test_all_six_templates_are_valid():
    from csv_schemas import SCHEMAS
    import csv as csv_module

    template_files = {
        "ground_truth_strategy": "ground_truth_strategy_template.csv",
        "detected_strategy": "detected_strategy_template.csv",
        "lap_performance": "lap_performance_template.csv",
        "control_latency": "control_latency_template.csv",
        "stability_run": "stability_run_template.csv",
        "fault_recovery": "fault_recovery_template.csv",
    }
    for kind, filename in template_files.items():
        path = TEMPLATES / filename
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv_module.DictReader(f))
        errors = validate_rows(rows, SCHEMAS[kind])
        assert errors == [], f"{filename} should be valid, got: {errors}"
