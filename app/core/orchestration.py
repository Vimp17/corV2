from __future__ import annotations

from app import db
from app.core.pipeline import analyze_well
from app.core.readiness import assess_readiness
from app.schemas import (
    FailureHistoryItem,
    PipelineResult,
    SkippedAnalysis,
    TelemetryPoint,
    WorkHistoryItem,
)


def analyze_saved_well(
    well_id: str,
    horizon_days: int,
    app_state,
    request_id: str | None = None,
    records_override: list[TelemetryPoint] | None = None,
    work_history_override: list[WorkHistoryItem] | None = None,
    failure_history_override: list[FailureHistoryItem] | None = None,
) -> PipelineResult | SkippedAnalysis:
    """Run readiness checks and, when the data is sufficient, the full analysis pipeline.

    Readiness decides alone whether models run. Stale telemetry still produces a result,
    flagged by the pipeline as stale with data_update_required=True.
    """
    records = records_override if records_override is not None else db.load_telemetry(well_id)
    source_rows = db.list_source_statuses(well_id)
    skipped = assess_readiness(well_id, records, source_rows, horizon_days)
    if skipped is not None:
        db.save_analysis_run(skipped.model_dump(mode="json"), request_id, horizon_days)
        return skipped

    history = db.load_history(well_id)
    work_history = work_history_override if work_history_override is not None else history["work_history"]
    failure_history = failure_history_override if failure_history_override is not None else history["failure_history"]

    result = analyze_well(
        well_id,
        records,
        horizon_days,
        app_state.cox_model,
        app_state.cox_model_error,
        model_registry=app_state.risk_model_registry,
        work_history=work_history,
        failure_history=failure_history,
    )
    db.save_analysis_run(result, request_id, horizon_days)
    return result
