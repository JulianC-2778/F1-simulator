#!/usr/bin/env python3
"""Validate a real-experiment CSV against its schema (commentary_test_plan.md
6.2). Used for ground-truth / detected-events / latency / stability_run /
fault_recovery files alike -- pick the schema with --kind.

Usage:
    python validate_experiment_data.py --kind ground_truth --file path.csv
    python validate_experiment_data.py --kind detected_events --file path.csv

Exit code 0 = valid, 1 = validation errors found (printed to stderr, one per
line, each naming the row it came from), 2 = usage/IO error (bad --kind,
file not found).
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schemas"))
from csv_schemas import SCHEMAS, KNOWN_EVENT_TYPES, CsvSchema  # noqa: E402


def _parse_number(raw: str, kind: str):
    if kind == "int":
        return int(raw)
    return float(raw)


def validate_rows(rows: list[dict], schema: CsvSchema) -> list[str]:
    errors: list[str] = []

    if not rows:
        errors.append("file has a header but zero data rows (empty dataset)")
        return errors

    header = set(rows[0].keys())
    missing = [c for c in schema.required_columns() if c not in header]
    if missing:
        errors.append(f"missing required column(s): {', '.join(missing)}")
        return errors  # further row checks would be meaningless without these

    seen_ids: dict[str, int] = {}
    for i, row in enumerate(rows, start=2):  # header is line 1
        for col in schema.columns:
            raw = row.get(col.name)
            if raw is None or raw == "":
                if col.required:
                    errors.append(f"row {i}: required field {col.name!r} is empty")
                continue
            if col.kind in ("int", "float"):
                try:
                    value = _parse_number(raw, col.kind)
                except (TypeError, ValueError):
                    errors.append(f"row {i}: field {col.name!r}={raw!r} is not a valid {col.kind}")
                    continue
                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    errors.append(f"row {i}: field {col.name!r}={raw!r} must be finite")
                    continue
                if col.name in schema.time_columns_nonnegative and value < 0:
                    errors.append(f"row {i}: field {col.name!r}={value} must be >= 0")

        if schema.start_end_columns:
            start_col, end_col = schema.start_end_columns
            start_raw, end_raw = row.get(start_col), row.get(end_col)
            if start_raw not in (None, "") and end_raw not in (None, ""):
                try:
                    start_v, end_v = float(start_raw), float(end_raw)
                    if end_v < start_v:
                        errors.append(
                            f"row {i}: {end_col}={end_v} must be >= {start_col}={start_v}"
                        )
                except (TypeError, ValueError):
                    pass  # already reported as a bad-number error above

        session = row.get(schema.session_column)
        if session in (None, ""):
            errors.append(f"row {i}: {schema.session_column!r} must not be empty")

        if schema.event_type_column:
            event_type = row.get(schema.event_type_column)
            if event_type and event_type not in KNOWN_EVENT_TYPES:
                errors.append(
                    f"row {i}: unrecognised event_type {event_type!r} "
                    f"(known: {', '.join(sorted(KNOWN_EVENT_TYPES))})"
                )

        if schema.id_column:
            row_id = row.get(schema.id_column)
            if row_id:
                if row_id in seen_ids:
                    errors.append(
                        f"row {i}: duplicate {schema.id_column}={row_id!r} "
                        f"(first seen at row {seen_ids[row_id]})"
                    )
                else:
                    seen_ids[row_id] = i

    return errors


def load_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def validate_file(path: Path, kind: str) -> list[str]:
    if kind not in SCHEMAS:
        raise SystemExit(f"unknown --kind {kind!r}; choices: {', '.join(sorted(SCHEMAS))}")
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    rows = load_csv(path)
    return validate_rows(rows, SCHEMAS[kind])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", required=True, choices=sorted(SCHEMAS))
    parser.add_argument("--file", required=True, type=Path)
    args = parser.parse_args()

    try:
        errors = validate_file(args.file, args.kind)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if errors:
        print(f"INVALID: {args.file} ({len(errors)} error(s))", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"OK: {args.file} is a valid {args.kind} CSV")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
