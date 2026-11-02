"""Daily cycle: refresh the configured source APIs, recompute the rating of every known well and list
the wells awaiting a decision (a measure without a planned date) — n8n asks the AI agent for a proposal
on each of them (POST /api/v1/rating/wells/{id}/ai-proposal)."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app import db
from app.config import settings
from app.core import rating, rating_builder
from app.core.source_connectors import refresh_sources_for_well
from app.schemas import MonitoringResult

logger = logging.getLogger(__name__)

SOURCE_FAILURE_STATUSES = {"error", "unavailable"}


class MonitorBusyError(RuntimeError):
    """Another monitor run holds the run lock."""


def refresh_well_sources(well_id: str) -> None:
    """Refresh one well's sources; a broken registry is recorded, never raised."""
    try:
        refresh_sources_for_well(well_id)
    except Exception as exc:
        logger.exception("Source refresh setup failed for well_id=%s", well_id)
        db.record_source_status("source_registry", well_id, "error",
                                f"Source profile or registry could not be loaded ({type(exc).__name__}).",
                                None, origin="system")


def _monitor() -> MonitoringResult:
    started = datetime.now(timezone.utc)
    well_ids = list(dict.fromkeys([*settings.configured_well_ids, *(row["well_id"] for row in db.list_wells())]))
    if not well_ids:
        return MonitoringResult(
            status="needs_configuration", started_at=started, completed_at=datetime.now(timezone.utc),
            wells_checked=0, wells_scored=0, wells_unscored=0, dashboard_url=settings.dashboard_url,
            message="Список скважин пуст. Укажите MAI_WELL_IDS или загрузите первые данные через API.")

    # Source refresh is network-bound, so wells are refreshed concurrently.
    with ThreadPoolExecutor(max_workers=settings.source_refresh_workers, thread_name_prefix="source-refresh") as pool:
        list(pool.map(refresh_well_sources, well_ids))

    failed: list[str] = []
    for well_id in well_ids:
        try:
            rating_builder.update(well_id)
        except Exception:
            logger.exception("Scheduled rating computation failed for well_id=%s", well_id)
            failed.append(well_id)
    checked = set(well_ids)
    wells = [well for well in rating.build_rating(db.list_rating_rows()) if well["well_id"] in checked]
    alerts = [{"well_id": well["well_id"], "rank": well["rank"], "index": well["index"],
               "measure_code": well["measure_code"], "measure": well["measure_label"], "deadline": well["deadline"],
               "dashboard_url": settings.dashboard_url} for well in wells if well["awaiting_decision"]]
    source_failures = [source for source in db.effective_source_statuses()
                       if source["well_id"] in checked and source["status"] in SOURCE_FAILURE_STATUSES]
    scored = sum(1 for well in wells if well["index"] is not None)
    message = (f"Проверено скважин: {len(well_ids)}. С оценкой: {scored}. "
               f"Ожидают решения: {len(alerts)}.")
    if failed:
        message += f" Ошибка расчёта: {len(failed)} скв., см. журнал backend."
    return MonitoringResult(
        status="completed", started_at=started, completed_at=datetime.now(timezone.utc),
        wells_checked=len(well_ids), wells_scored=scored, wells_unscored=len(wells) - scored, alerts=alerts,
        source_failures=source_failures + [{"well_id": well_id, "source_id": "analysis", "status": "error",
                                            "message": "Rating computation failed; inspect backend logs."}
                                           for well_id in failed],
        dashboard_url=settings.dashboard_url, message=message)


def run_monitor() -> MonitoringResult:
    """Run the cycle under a database-wide lock so two runs never overlap."""
    with db.advisory_lock(db.MONITOR_LOCK_KEY) as acquired:
        if not acquired:
            raise MonitorBusyError("Another monitor run is already in progress.")
        return _monitor()


def run_monitor_job(job_id: str) -> None:
    """Background-task wrapper: records progress and the result in monitor_jobs."""
    db.update_monitor_job(job_id, status="running", started_at=datetime.now(timezone.utc))
    try:
        result = run_monitor()
    except MonitorBusyError as exc:
        db.update_monitor_job(job_id, status="rejected", error=str(exc), completed_at=datetime.now(timezone.utc))
    except Exception as exc:
        logger.exception("Monitor job failed job_id=%s", job_id)
        db.update_monitor_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:2000],
                              completed_at=datetime.now(timezone.utc))
    else:
        db.update_monitor_job(job_id, status="completed", result_json=result.model_dump(mode="json"),
                              completed_at=datetime.now(timezone.utc))
