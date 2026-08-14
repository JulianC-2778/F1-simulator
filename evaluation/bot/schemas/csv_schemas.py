"""Column schemas for every CSV format work packages B/C/D of
`docs/bot_test_plan.md` produce or consume. Declarative -- `validate_experiment_data.py`,
`match_strategy_decisions.py`, `analyse_control_latency.py`, `analyse_stability.py`
and `generate_report_tables.py` all import these instead of hard-coding
column lists, so the schema has exactly one source of truth.

Direct counterpart of `evaluation/commentary/schemas/csv_schemas.py` -- see
`docs/bot_test_plan.md` section 0 for why the two directions share one
evaluation methodology.

Strategy vocabulary (`ai_bot.py` L627-662): ATTACK, NORMAL, DEFEND,
SAVE_FUEL, PIT are the five values Granite is ever offered a choice
between (`_GRANITE_STRATEGIES`); BLOCK is system-only, produced solely by
`safety_filter`'s rear-gap rule, and must never be reachable from Granite's
own text output (`ai_bot.py` L1980, L2027, L2119; regression-tested in
`tests/bot/test_safety_integration.py::BlockIsSystemOnlyEndToEndTests`).
`raw_granite_strategy` in `detected_strategy` is deliberately NOT validated
against this vocabulary here -- it is allowed to contain hallucinated or
malformed text, because proving the parse layer rejects exactly that is the
point of work package B's strategy-accuracy comparison. `filtered_strategy`
(safety_filter's output) IS validated, since safety_filter guarantees an
`_ALL_STRATEGIES` member.
"""

from __future__ import annotations

from dataclasses import dataclass

KNOWN_STRATEGIES = {
    "ATTACK",
    "NORMAL",
    "DEFEND",
    "SAVE_FUEL",
    "PIT",
    "BLOCK",
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
    strategy_column: str | None = None  # validated against KNOWN_STRATEGIES if set
    time_columns_nonnegative: tuple[str, ...] = ()
    start_end_columns: tuple[str, str] | None = None  # (start, end): end >= start

    def required_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.required]

    def column_kind(self, name: str) -> str | None:
        for c in self.columns:
            if c.name == name:
                return c.kind
        return None


# Work package B, part 1: human-annotated "what should the strategy have
# been" readings -- bot_test_plan.md 5.2's first CSV.
GROUND_TRUTH_STRATEGY_SCHEMA = CsvSchema(
    name="ground_truth_strategy",
    columns=[
        ColumnSpec("session", "str"),
        ColumnSpec("reading_id", "str"),
        ColumnSpec("timestamp_s", "float"),
        ColumnSpec("fuel_L", "float"),
        ColumnSpec("damage", "float"),
        ColumnSpec("rear_gap_m", "float", required=False),
        ColumnSpec("expected_strategy", "str"),
        ColumnSpec("annotator", "str", required=False),
    ],
    id_column="reading_id",
    strategy_column="expected_strategy",
    time_columns_nonnegative=("timestamp_s", "fuel_L", "damage", "rear_gap_m"),
)

# Work package B, part 2: what the bot actually decided -- bot_test_plan.md
# 5.2's second CSV. raw_granite_strategy is intentionally not in
# KNOWN_STRATEGIES's validated set (see module docstring).
DETECTED_STRATEGY_SCHEMA = CsvSchema(
    name="detected_strategy",
    columns=[
        ColumnSpec("session", "str"),
        ColumnSpec("decision_id", "str"),
        ColumnSpec("timestamp_s", "float"),
        ColumnSpec("raw_granite_strategy", "str"),
        ColumnSpec("filtered_strategy", "str"),
        ColumnSpec("source", "str", required=False),
    ],
    id_column="decision_id",
    strategy_column="filtered_strategy",
    time_columns_nonnegative=("timestamp_s",),
)

# Work package B, bot-autonomous objective driving-quality summary --
# bot_test_plan.md 5.3. One row per session; unlike commentary there is no
# ground-truth/detection pair here, just aggregate outcomes (produced either
# by hand from a real session or via evaluation/bot/scripts/analyse_bot_trace.py
# on a real TraceRecorder JSONL log, then copied in as one row per session).
LAP_PERFORMANCE_SCHEMA = CsvSchema(
    name="lap_performance",
    columns=[
        ColumnSpec("session", "str"),
        ColumnSpec("track", "str"),
        ColumnSpec("granite_enabled", "str"),
        ColumnSpec("laps_target", "int"),
        ColumnSpec("laps_completed", "int"),
        ColumnSpec("completed", "str"),
        ColumnSpec("distance_km", "float"),
        ColumnSpec("off_track_excursions", "int"),
        ColumnSpec("off_track_recoveries", "int"),
        ColumnSpec("collisions", "int"),
        ColumnSpec("lap_time_mean_s", "float", required=False),
        ColumnSpec("lap_time_stdev_s", "float", required=False),
        ColumnSpec("strategy_switches", "int", required=False),
        ColumnSpec("notes", "str", required=False),
    ],
    id_column="session",
    time_columns_nonnegative=("distance_km", "lap_time_mean_s", "lap_time_stdev_s"),
)

# Work package C: two independent latency chains in one CSV, discriminated
# by which columns are populated per row (bot_test_plan.md 6.1) -- mirrors
# LATENCY_SCHEMA's "many optional timestamp columns" shape rather than
# splitting into two files, so analyse_control_latency.py can compute every
# metric with one pass regardless of row kind.
CONTROL_LATENCY_SCHEMA = CsvSchema(
    name="control_latency",
    columns=[
        ColumnSpec("session", "str"),
        ColumnSpec("record_id", "str"),
        ColumnSpec("kind", "str"),  # "frame" | "granite_rtt"
        # Control loop, per-frame (u0-u2).
        ColumnSpec("u0_scr_state_received", "float", required=False),
        ColumnSpec("u1_control_computed", "float", required=False),
        ColumnSpec("u2_control_sent", "float", required=False),
        # Granite strategy call, per-request (g0-g3).
        ColumnSpec("g0_state_snapshot", "float", required=False),
        ColumnSpec("g1_first_byte", "float", required=False),
        ColumnSpec("g2_response_complete", "float", required=False),
        ColumnSpec("g3_strategy_applied", "float", required=False),
        ColumnSpec("failed", "str", required=False),
        ColumnSpec("failure_reason", "str", required=False),
    ],
    id_column="record_id",
    strategy_column=None,
    time_columns_nonnegative=(
        "u0_scr_state_received", "u1_control_computed", "u2_control_sent",
        "g0_state_snapshot", "g1_first_byte", "g2_response_complete", "g3_strategy_applied",
    ),
)

# Work package D, endurance -- bot_test_plan.md 7.1. Field names are the
# bot-specific counterparts of commentary's STABILITY_RUN_SCHEMA
# (events_detected -> control_frames_processed, commentary_requests ->
# strategy_requests, no successful_outputs/duplicate_displays concept for a
# driving loop; safety_filter_interventions and its per-type breakdown are
# new, since "how often did the safety net have to override Granite" is the
# bot-specific stability signal commentary has no analogue for).
STABILITY_RUN_SCHEMA = CsvSchema(
    name="stability_run",
    columns=[
        ColumnSpec("run_id", "str"),
        ColumnSpec("duration_s", "float"),
        ColumnSpec("granite_enabled", "str", required=False),
        ColumnSpec("control_frames_processed", "int"),
        ColumnSpec("strategy_requests", "int"),
        ColumnSpec("granite_failures", "int"),
        ColumnSpec("safety_filter_interventions", "int"),
        ColumnSpec("safety_filter_pit_count", "int", required=False),
        ColumnSpec("safety_filter_defend_count", "int", required=False),
        ColumnSpec("safety_filter_block_count", "int", required=False),
        ColumnSpec("safety_filter_normal_cap_count", "int", required=False),
        ColumnSpec("collisions", "int"),
        ColumnSpec("off_track_excursions", "int"),
        ColumnSpec("recoveries", "int"),
        ColumnSpec("unhandled_exceptions", "int"),
        ColumnSpec("crashed", "str", required=False),
        ColumnSpec("cpu_avg_pct", "float", required=False),
        ColumnSpec("cpu_peak_pct", "float", required=False),
        ColumnSpec("mem_initial_mb", "float", required=False),
        ColumnSpec("mem_final_mb", "float", required=False),
        ColumnSpec("mem_peak_mb", "float", required=False),
    ],
    id_column="run_id",
    session_column="run_id",
    strategy_column=None,
    time_columns_nonnegative=("duration_s",),
)

# Work package D, fault injection -- bot_test_plan.md 7.2, RB-01..RB-10.
# Same shape as commentary's FAULT_RECOVERY_SCHEMA (proven format, just a
# different fault_id vocabulary); fault_id is deliberately not restricted to
# a fixed set here either, for the same reason commentary's isn't -- new
# fault kinds shouldn't require a schema change to record.
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
    strategy_column=None,
    time_columns_nonnegative=(
        "fault_injected_at_s", "service_restored_at_s", "first_success_after_restore_at_s",
    ),
)

SCHEMAS = {
    schema.name: schema
    for schema in (
        GROUND_TRUTH_STRATEGY_SCHEMA,
        DETECTED_STRATEGY_SCHEMA,
        LAP_PERFORMANCE_SCHEMA,
        CONTROL_LATENCY_SCHEMA,
        STABILITY_RUN_SCHEMA,
        FAULT_RECOVERY_SCHEMA,
    )
}
