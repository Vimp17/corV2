"""Scheduled risk monitor: refresh sources, analyse every known well, collect alerts."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app import db
from app.config import settings
from app.core.orchestration import analyze_saved_well_horizons
from app.core.source_connectors import refresh_sources_for_well
from app.schemas import MonitoringResult, PipelineResult, SkippedAnalysis

logger = logging.getLogger(__name__)

RISK_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "UNKNOWN": 4,
    "DATA_ISSUE": 5,
    "NOT_ANALYZED": 6,
}
SOURCE_FAILURE_STATUSES = {"error", "unavailable"}
DEFAULT_HORIZONS = (30, 90, 180)


class MonitorBusyError(RuntimeError):
    """Another monitor run holds the run lock."""


def probability_alert(result: PipelineResult) -> tuple[bool, float | None, str | None]:
    eligible = [
        item for item in result.model_predictions
        if item.status == "ok" and item.calibrated and item.probability is not None
    ]
    if not eligible:
        return False, None, None
    best = max(eligible, key=lambda item: item.probability or 0)
    return (
        best.probability >= settings.alert_probability_threshold,
        best.probability,
        best.model_id,
    )


def is_alert(result: PipelineResult) -> tuple[bool, str]:
    risk_class = result.rule_risk.risk_class.upper()
    if risk_class in settings.alert_rule_classes:
        return True, f"Rule Engine: {risk_class} ({result.rule_risk.risk_points or 0} баллов)"
    alert, probability, model_id = probability_alert(result)
    if alert:
        return True, f"{model_id}: калиброванная вероятность {probability:.1%} за {result.cox_risk.horizon_days} дней"
    return False, ""


def select_well_alert(candidates: list[dict], requested_horizon: int) -> dict:
    """Pick one alert per well: the requested horizon if it alerted, else the most severe."""
    chosen = next((item for item in candidates if item["horizon_days"] == requested_horizon), None)
    if chosen is None:
        chosen = min(candidates, key=lambda item: (
            RISK_ORDER.get(str(item["risk_class"]).upper(), 99),
            -(item["risk_score"] or 0),
            item["horizon_days"],
        ))
    return {**chosen, "alert_horizons": sorted(item["horizon_days"] for item in candidates)}


def refresh_well_sources(well_id: str) -> None:
    """Refresh one well's sources; a broken registry is recorded, never raised."""
    try:
        refresh_sources_for_well(well_id)
    except Exception as exc:
        logger.exception("Source refresh setup failed for well_id=%s", well_id)
        db.record_source_status(
            "source_registry",
            well_id,
            "error",
            f"Source profile or registry could not be loaded ({type(exc).__name__}).",
            None,
            origin="system",
        )


def _monitor(horizon_days: int, app_state, request_id: str | None) -> MonitoringResult:
    started = datetime.now(timezone.utc)
    well_ids = list(dict.fromkeys([
        *settings.configured_well_ids,
        *(row["well_id"] for row in db.list_wells()),
    ]))
    if not well_ids:
        return MonitoringResult(
            status="needs_configuration",
            started_at=started,
            completed_at=datetime.now(timezone.utc),
            wells_checked=0,
            analyses_completed=0,
            analyses_skipped=0,
            alerts=[],
            source_failures=[],
            dashboard_url=settings.dashboard_url,
            message="Список скважин пуст. Укажите MAI_WELL_IDS или загрузите первые данные через API.",
        )

    # Phase 1: source refresh is network-bound, so wells are refreshed concurrently.
    with ThreadPoolExecutor(max_workers=settings.source_refresh_workers,
                            thread_name_prefix="source-refresh") as pool:
        list(pool.map(refresh_well_sources, well_ids))

    # Phase 2: analysis is CPU-bound Python; each well is prepared once for all horizons.
    horizons = sorted({*DEFAULT_HORIZONS, horizon_days})
    alert_candidates: dict[str, list[dict]] = {}
    source_failures: list[dict] = []
    completed = skipped = 0
    skipped_wells: set[str] = set()
    failed_wells: set[str] = set()

    for well_id in well_ids:
        try:
            results = analyze_saved_well_horizons(well_id, horizons, app_state, request_id)
        except Exception as exc:
            logger.exception("Scheduled analysis failed for well_id=%s", well_id)
            failed_wells.add(well_id)
            source_failures.append({
                "well_id": well_id,
                "source_id": "analysis",
                "status": "error",
                "message": f"Analysis failed ({type(exc).__name__}); inspect backend logs.",
            })
            results = []

        for result in results:
            if isinstance(result, SkippedAnalysis):
                skipped += 1
                skipped_wells.add(well_id)
                continue
            completed += 1
            risky, reason = is_alert(result)
            if risky:
                alert_candidates.setdefault(well_id, []).append({
                    "well_id": well_id,
                    "analysis_id": result.analysis_id,
                    "risk_class": result.rule_risk.risk_class,
                    "risk_score": result.rule_risk.risk_score,
                    "risk_points": result.rule_risk.risk_points,
                    "horizon_days": result.cox_risk.horizon_days,
                    "reason": reason,
                    "dashboard_url": settings.dashboard_url,
                })

        # Source failures are a property of the well, not of the horizon: report them once.
        for source in db.effective_source_statuses(well_id):
            if source["status"] in SOURCE_FAILURE_STATUSES:
                source_failures.append({"well_id": well_id, **source})

    alerts = [select_well_alert(candidates, horizon_days) for candidates in alert_candidates.values()]
    try:
        pruned = db.prune_analysis_runs(settings.analysis_retention_days)
        if pruned:
            logger.info("Pruned %d analysis runs older than %d days", pruned,
                        settings.analysis_retention_days)
    except Exception:
        logger.exception("Analysis history pruning failed")

    message = (
        f"Проверено скважин: {len(well_ids)}. Скважин с высоким/критическим риском: {len(alerts)}. "
        f"Скважин без расчёта из-за данных: {len(skipped_wells)}. Горизонты: {horizons}."
    )
    if failed_wells:
        message += f" Ошибка расчёта: {len(failed_wells)} скв., см. журнал backend."
    return MonitoringResult(
        status="completed",
        started_at=started,
        completed_at=datetime.now(timezone.utc),
        wells_checked=len(well_ids),
        analyses_completed=completed,
        analyses_skipped=skipped,
        alerts=alerts,
        source_failures=source_failures,
        dashboard_url=settings.dashboard_url,
        message=message,
    )


def run_monitor(horizon_days: int, app_state, request_id: str | None) -> MonitoringResult:
    """Run the monitor under a database-wide lock so two runs never overlap."""
    with db.advisory_lock(db.MONITOR_LOCK_KEY) as acquired:
        if not acquired:
            raise MonitorBusyError("Another monitor run is already in progress.")
        return _monitor(horizon_days, app_state, request_id)


def run_monitor_job(job_id: str, horizon_days: int, app_state) -> None:
    """Background-task wrapper: records progress and the result in monitor_jobs."""
    db.update_monitor_job(job_id, status="running", started_at=datetime.now(timezone.utc))
    try:
        # The job id doubles as the request id, so AI reports reach every horizon run.
        result = run_monitor(horizon_days, app_state, job_id)
    except MonitorBusyError as exc:
        db.update_monitor_job(job_id, status="rejected", error=str(exc),
                              completed_at=datetime.now(timezone.utc))
    except Exception as exc:
        logger.exception("Monitor job failed job_id=%s", job_id)
        db.update_monitor_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:2000],
                              completed_at=datetime.now(timezone.utc))
    else:
        db.update_monitor_job(job_id, status="completed", result_json=result.model_dump(mode="json"),
                              completed_at=datetime.now(timezone.utc))
