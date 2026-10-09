from __future__ import annotations

import uuid

from app import db
from app.core.pipeline import analyze_prepared, prepare_well_analysis
from app.core.readiness import assess_readiness
from app.schemas import (
    FailureHistoryItem,
    PipelineResult,
    SkippedAnalysis,
    TelemetryPoint,
    WorkHistoryItem,
)


def analyze_saved_well_horizons(
    well_id: str,
    horizons: list[int],
    app_state,
    request_id: str | None = None,
    records_override: list[TelemetryPoint] | None = None,
    work_history_override: list[WorkHistoryItem] | None = None,
    failure_history_override: list[FailureHistoryItem] | None = None,
) -> list[PipelineResult | SkippedAnalysis]:
    """Analyse one well on several horizons, loading and preparing its data only once.

    Readiness decides alone whether models run. Stale telemetry still produces results,
    flagged by the pipeline as stale with data_update_required=True. Data loading, data
    quality, normalisation, features and diagnostics do not depend on the horizon; only the
    risk models are re-run per horizon. One run is stored per horizon for the dashboard.
    """
    horizons = list(dict.fromkeys(horizons))
    records = records_override if records_override is not None else db.load_telemetry(well_id)
    source_rows = db.list_source_statuses(well_id)

    skipped = assess_readiness(well_id, records, source_rows)
    if skipped is not None:
        results: list[PipelineResult | SkippedAnalysis] = []
        for horizon_days in horizons:
            per_horizon = skipped.model_copy(
                update={"horizon_days": horizon_days, "analysis_id": str(uuid.uuid4())}, deep=True)
            db.save_analysis_run(per_horizon.model_dump(mode="json"), request_id, horizon_days)
            results.append(per_horizon)
        return results

    history = db.load_history(well_id)
    work_history = work_history_override if work_history_override is not None else history["work_history"]
    failure_history = failure_history_override if failure_history_override is not None else history["failure_history"]

    prepared = prepare_well_analysis(well_id, records)
    results = []
    for horizon_days in horizons:
        result = analyze_prepared(
            prepared, horizon_days, app_state.cox_model, app_state.cox_model_error,
            model_registry=app_state.risk_model_registry,
            work_history=work_history, failure_history=failure_history,
        )
        db.save_analysis_run(result, request_id, horizon_days)
        results.append(result)
    return results


def analyze_saved_well(
    well_id: str,
    horizon_days: int,
    app_state,
    request_id: str | None = None,
    records_override: list[TelemetryPoint] | None = None,
    work_history_override: list[WorkHistoryItem] | None = None,
    failure_history_override: list[FailureHistoryItem] | None = None,
) -> PipelineResult | SkippedAnalysis:
    return analyze_saved_well_horizons(
        well_id, [horizon_days], app_state, request_id,
        records_override, work_history_override, failure_history_override,
    )[0]
