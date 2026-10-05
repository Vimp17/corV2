from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db
from app.config import settings
from app.core.pipeline import analyze_well
from app.core.readiness import assess_readiness
from app.schemas import (
    FailureHistoryItem,
    PipelineResult,
    SkippedAnalysis,
    TelemetryPoint,
    WorkHistoryItem,
)

_MIN_RECORDS = getattr(settings, "min_telemetry_records", 8)
_MAX_AGE_HOURS = getattr(settings, "max_telemetry_age_hours", 48)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _last_age_hours(records: list[TelemetryPoint]) -> float | None:
    if not records:
        return None
    last = max(_utc(point.timestamp) for point in records)
    return (datetime.now(timezone.utc) - last).total_seconds() / 3600.0


def _last_in_future(records: list[TelemetryPoint]) -> bool:
    if not records:
        return False
    last = max(_utc(point.timestamp) for point in records)
    return last > datetime.now(timezone.utc) + timedelta(hours=24)


def _overall_volume_ok(skipped: SkippedAnalysis) -> bool:
    return (
        skipped.telemetry_records >= _MIN_RECORDS
        and skipped.unique_timestamps >= _MIN_RECORDS
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
    records = records_override if records_override is not None else db.load_telemetry(well_id)
    source_rows = db.list_source_statuses(well_id)
    skipped = assess_readiness(well_id, records, source_rows)

    if skipped is not None:
        stale_override = (
            bool(records)
            and _overall_volume_ok(skipped)
            and not _last_in_future(records)
        )
        if not stale_override:
            db.save_analysis_run(skipped.model_dump(mode="json"), request_id)
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

    age_hours = _last_age_hours(records)
    if age_hours is not None and age_hours > _MAX_AGE_HOURS:
        result.context.data_freshness = "stale"
        result.context.telemetry_age_hours = age_hours
        result.data_update_required = True

    db.save_analysis_run(result, request_id)
    return result

def analyze_saved_well_all_horizons(
    well_id: str,
    app_state,
    request_id: str | None = None,
    horizons: list[int] | None = None,
    records_override: list[TelemetryPoint] | None = None,
    work_history_override: list[WorkHistoryItem] | None = None,
    failure_history_override: list[FailureHistoryItem] | None = None,
) -> list[PipelineResult | SkippedAnalysis]:
    """Analyze a well on multiple horizons (default: 30, 90, 180 days)."""
    if horizons is None:
        horizons = [30, 90, 180]
    
    results = []
    for horizon_days in horizons:
        result = analyze_saved_well(
            well_id,
            horizon_days,
            app_state,
            request_id,
            records_override,
            work_history_override,
            failure_history_override,
        )
        results.append(result)
    return results