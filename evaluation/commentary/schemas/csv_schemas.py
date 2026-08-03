"""Column schemas for every CSV format work packages B/C/D produce or
consume. Declarative -- `evaluation/commentary/scripts/validate_experiment_data.py`
and `match_events.py` both import these instead of hard-coding column lists,
so the schema has exactly one source of truth.

Real event-type vocabulary (docs/commentary_test_matrix.md section 2):
contact, position_change, off_track, lap_complete, battle, pace_surge,
pace_update. `pace_update` is excluded from KNOWN_EVENT_TYPES for ground
truth / detections on purpose -- commentary_test_plan.md 6.1 says it's
checked separately for trigger interval and "does not count toward event
F1".
"""

from __future__ import annotations

from dataclasses import dataclass, field

KNOWN_EVENT_TYPES = {
    "contact",
    "position_change",
    "off_track",
    "lap_complete",
    "battle",
    "pace_surge",
}


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    kind: str  # "str" | "float" | "int"
    required: bool = True


@dataclass(frozen=True)
class CsvSchema:
    name: str
    columns: list[ColumnSpec]
    id_column: str | None = None  # must be unique within the file
    session_column: str = "session"
    event_type_column: str | None = "event_type"
    time_columns_nonnegative: tuple[str, ...] = ()
    start_end_columns: tuple[str, str] | None = None  # (start, end): end >= start

    def required_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.required]

    def column_kind(self, name: str) -> str | None:
        for c in self.columns:
            if c.name == name:
                return c.kind
        return None


GROUND_TRUTH_SCHEMA = CsvSchema(
    name="ground_truth",
    columns=[
        ColumnSpec("session", "str"),
        ColumnSpec("event_id", "str"),
        ColumnSpec("event_type", "str"),
        ColumnSpec("start_time_s", "float"),
        ColumnSpec("end_time_s", "float"),
        ColumnSpec("description", "str", required=False),
        ColumnSpec("annotator", "str", required=False),
    ],
    id_column="event_id",
    time_columns_nonnegative=("start_time_s", "end_time_s"),
    start_end_columns=("start_time_s", "end_time_s"),
)

DETECTED_EVENTS_SCHEMA = CsvSchema(
    name="detected_events",
    columns=[
        ColumnSpec("session", "str"),
        ColumnSpec("detection_id", "str"),
        ColumnSpec("event_type", "str"),
        ColumnSpec("detection_time_s", "float"),
        ColumnSpec("priority", "int", required=False),
        ColumnSpec("source", "str", required=False),
    ],
    id_column="detection_id",
    time_columns_nonnegative=("detection_time_s",),
)

LATENCY_SCHEMA = CsvSchema(
    name="latency",
    columns=[
        ColumnSpec("session_id", "str"),
        ColumnSpec("event_id", "str"),
        ColumnSpec("request_id", "str"),
        ColumnSpec("t0_telemetry_received", "float", required=False),
        ColumnSpec("t1_event_detected", "float", required=False),
        ColumnSpec("t2_first_token", "float", required=False),
        ColumnSpec("t3_ai_done", "float", required=False),
        ColumnSpec("t4_caption_displayed", "float", required=False),
        ColumnSpec("t5_tts_started", "float", required=False),
        ColumnSpec("failed", "str", required=False),
        ColumnSpec("failure_reason", "str", required=False),
    ],
    id_column="request_id",
    session_column="session_id",
    event_type_column=None,
    time_columns_nonnegative=(
        "t0_telemetry_received", "t1_event_detected", "t2_first_token",
        "t3_ai_done", "t4_caption_displayed", "t5_tts_started",
    ),
)

STABILITY_RUN_SCHEMA = CsvSchema(
    name="stability_run",
    columns=[
        ColumnSpec("run_id", "str"),
        ColumnSpec("duration_s", "float"),
        ColumnSpec("events_detected", "int"),
        ColumnSpec("commentary_requests", "int"),
        ColumnSpec("successful_outputs", "int"),
        ColumnSpec("model_failures", "int"),
        ColumnSpec("duplicate_user_visible_displays", "int"),
        ColumnSpec("unhandled_exceptions", "int"),
        ColumnSpec("reconnect_recovery_time_s", "float", required=False),
        ColumnSpec("cpu_avg_pct", "float", required=False),
        ColumnSpec("cpu_peak_pct", "float", required=False),
        ColumnSpec("mem_initial_mb", "float", required=False),
        ColumnSpec("mem_final_mb", "float", required=False),
        ColumnSpec("mem_peak_mb", "float", required=False),
        ColumnSpec("outputs_total", "int", required=False),
        ColumnSpec("outputs_over_45_words", "int", required=False),
    ],
    id_column="run_id",
    session_column="run_id",
    event_type_column=None,
    time_columns_nonnegative=("duration_s", "reconnect_recovery_time_s"),
)

FAULT_RECOVERY_SCHEMA = CsvSchema(
    name="fault_recovery",
    columns=[
        ColumnSpec("trial_id", "str"),
        ColumnSpec("fault_id", "str"),
        ColumnSpec("fault_injected_at_s", "float"),
        ColumnSpec("service_restored_at_s", "float", required=False),
        ColumnSpec("first_success_after_restore_at_s", "float", required=False),
        ColumnSpec("recovered", "str"),
        ColumnSpec("crashed", "str"),
        ColumnSpec("notes", "str", required=False),
    ],
    id_column="trial_id",
    session_column="fault_id",
    event_type_column=None,
    time_columns_nonnegative=(
        "fault_injected_at_s", "service_restored_at_s", "first_success_after_restore_at_s",
    ),
)

SCHEMAS = {
    schema.name: schema
    for schema in (
        GROUND_TRUTH_SCHEMA,
        DETECTED_EVENTS_SCHEMA,
        LATENCY_SCHEMA,
        STABILITY_RUN_SCHEMA,
        FAULT_RECOVERY_SCHEMA,
    )
}
