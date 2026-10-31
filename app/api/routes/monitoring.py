from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse

from app import db
from app.api.routes.rating import rated_well
from app.config import settings
from app.core import rating_builder
from app.core.monitor_runner import MonitorBusyError, refresh_well_sources, run_monitor, run_monitor_job
from app.schemas import MonitoringResult

router = APIRouter(tags=["monitoring"])


@router.post("/monitor/wells/{well_id}/analyze")
def analyze_one_well(well_id: str) -> dict:
    """The daily cycle for one well: pull its configured source APIs and recompute its rating.
    Returns the well as in the rating (data_gaps tell what is short when it has no index). A well
    with no data in the database or the sources is not added (404): a mistyped number stays out."""
    well_id = well_id.strip()
    if not well_id or len(well_id) > 128:
        raise HTTPException(status_code=422, detail="well_id must contain 1 to 128 characters")
    known = well_id in settings.configured_well_ids or db.get_rating_row(well_id) is not None
    refresh_well_sources(well_id)
    if not known and not db.well_has_data(well_id):
        db.forget_well(well_id)  # the source statuses the refresh has just recorded
        raise HTTPException(status_code=404, detail="Скважина не найдена: данных по ней нет ни в базе, ни в источниках")
    rating_builder.update(well_id)
    return rated_well(well_id)


@router.post("/monitor/run", response_model=MonitoringResult,
             responses={202: {"description": "Background job accepted"},
                        409: {"description": "Another monitor run is in progress"}})
def run_scheduled_monitor(background_tasks: BackgroundTasks, background: bool = Query(default=False, description=(
        "Run as a background job and return 202 with job_id; poll /monitor/jobs/{job_id}. "
        "Without it the call blocks until the run completes."))):
    if background:
        job_id = str(uuid.uuid4())
        job = db.create_monitor_job(job_id)
        background_tasks.add_task(run_monitor_job, job_id)
        return JSONResponse(status_code=202, content={**job, "status_url": f"/api/v1/monitor/jobs/{job_id}"})
    try:
        return run_monitor()
    except MonitorBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/monitor/jobs/{job_id}")
def get_monitor_job(job_id: str) -> dict:
    job = db.get_monitor_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Monitor job was not found")
    return job
