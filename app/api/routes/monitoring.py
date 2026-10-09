from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app import db
from app.config import settings
from app.core.monitor_runner import (
    RISK_ORDER, MonitorBusyError, refresh_well_sources, run_monitor, run_monitor_job,
)
from app.core.orchestration import analyze_saved_well
from app.core.readiness import source_status_contract
from app.schemas import (
    DashboardSnapshot,
    ExternalWellData,
    MonitorRequest,
    MonitoringResult,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["monitoring", "dashboard"])

_DASHBOARD_DATA_ISSUE_SOURCE_STATUSES = {"error", "unavailable"}


def _dashboard_row(
    run: dict,
    well_summary: dict | None = None,
    source_statuses: list[dict] | None = None,
) -> dict:
    analysis = run.get("analysis") or {}
    agent = run.get("agent_response") or {}
    skipped = analysis.get("status") == "skipped_insufficient_data"
    rule = analysis.get("rule_risk") or {}
    risk_class = "DATA_ISSUE" if skipped else str(rule.get("risk_class", "UNKNOWN")).upper()
    predictions = analysis.get("model_predictions", [])
    calibrated = [
        item.get("probability") for item in predictions
        if item.get("status") == "ok" and item.get("calibrated")
        and item.get("probability") is not None
    ]
    if not skipped and calibrated and max(calibrated) >= settings.alert_probability_threshold:
        if RISK_ORDER.get(risk_class, 99) > RISK_ORDER["HIGH"]:
            risk_class = "HIGH"
    findings = (rule.get("findings") or [])[:5]
    missing = analysis.get("missing_items") or []
    return {
        "well_id": run["well_id"],
        "analysis_id": run.get("analysis_id"),
        "updated_at": run.get("created_at"),
        "status": analysis.get("status", run.get("status")),
        "risk_class": risk_class,
        "risk_points": rule.get("risk_points"),
        "risk_score": rule.get("risk_score"),
        "horizon_days": run.get("horizon_days") or analysis.get("horizon_days") or (
            predictions[0].get("horizon_days") if predictions else None),
        "model_predictions": predictions,
        "ai_status": agent.get("status", run.get("agent_status") or "pending"),
        "ai_updated_at": run.get("agent_updated_at"),
        "ai_report": agent.get("report"),
        "reasons": [item.get("reason") for item in findings if item.get("reason")],
        "missing_items": missing,
        "source_statuses": source_statuses or [],
        "telemetry_records": analysis.get(
            "telemetry_records", (well_summary or {}).get("record_count", 0)
        ),
        "data_freshness": (analysis.get("context") or {}).get("data_freshness"),
        "data_update_required": bool(analysis.get("data_update_required")),
    }


def _ranking() -> list[dict]:
    """One row per (well, horizon): the latest run for each pair, plus unanalysed wells."""
    summaries = {row["well_id"]: row for row in db.list_wells()}
    source_map: dict[str, list[dict]] = {}
    for source in db.effective_source_statuses():
        source_map.setdefault(source["well_id"], []).append(source)

    runs = db.latest_analysis_runs()
    # Runs saved before horizon_days was recorded (legacy skipped runs) have no horizon.
    # Show such a row only while the well has no newer horizon-specific run.
    newest_with_horizon: dict[str, str] = {}
    for run in runs:
        if run["horizon_days"] is not None:
            current = newest_with_horizon.get(run["well_id"])
            if current is None or run["created_at"] > current:
                newest_with_horizon[run["well_id"]] = run["created_at"]
    rows = []
    for run in runs:
        if run["horizon_days"] is None:
            newer = newest_with_horizon.get(run["well_id"])
            if newer is not None and newer >= run["created_at"]:
                continue
        rows.append(_dashboard_row(run, summaries.get(run["well_id"]),
                                   source_map.get(run["well_id"], [])))

    analysed = {row["well_id"] for row in rows}
    for well_id, summary in summaries.items():
        if well_id in analysed:
            continue
        rows.append({
            "well_id": well_id,
            "analysis_id": None,
            "updated_at": None,
            "status": "not_analyzed",
            "risk_class": "NOT_ANALYZED",
            "risk_points": None,
            "risk_score": None,
            "horizon_days": None,
            "reasons": [],
            "missing_items": ["Запустите анализ для этой скважины."],
            "source_statuses": source_map.get(well_id, []),
            "telemetry_records": summary.get("record_count", 0),
            "data_freshness": None,
            "data_update_required": False,
            "model_predictions": [],
            "ai_status": "pending",
            "ai_updated_at": None,
            "ai_report": None,
        })

    rows.sort(
        key=lambda row: (
            row.get("horizon_days") or 0,
            RISK_ORDER.get(row["risk_class"], 99),
            -(row["risk_score"] if row["risk_score"] is not None else -1),
            row["well_id"],
        )
    )
    return rows


@router.get("/dashboard/risk-ranking", response_model=DashboardSnapshot)
def risk_ranking() -> DashboardSnapshot:
    ranking = _ranking()
    updated_at = [row["updated_at"] for row in ranking if row.get("updated_at")]
    last_analysis_at = max(updated_at) if updated_at else None
    # Counters are per well: the ranking has one row per (well, horizon) pair.
    return DashboardSnapshot(
        generated_at=datetime.now(timezone.utc),
        last_analysis_at=last_analysis_at,
        wells_total=len({row["well_id"] for row in ranking}),
        at_risk_count=len({
            row["well_id"] for row in ranking
            if row["risk_class"] in settings.alert_rule_classes
        }),
        data_issue_count=len({
            row["well_id"] for row in ranking
            if row["risk_class"] == "DATA_ISSUE" or any(
                source["status"] in _DASHBOARD_DATA_ISSUE_SOURCE_STATUSES
                for source in row.get("source_statuses", [])
            )
        }),
        ranking=ranking,
    )


@router.post("/monitor/wells/{well_id}/analyze")
def analyze_one_well(well_id: str, request_data: MonitorRequest, request: Request):
    well_id = well_id.strip()
    if not well_id or len(well_id) > 128:
        raise HTTPException(status_code=422, detail="well_id must contain 1 to 128 characters")
    refresh_well_sources(well_id)
    return analyze_saved_well(
        well_id,
        request_data.horizon_days,
        request.app.state,
        getattr(request.state, "request_id", None),
    )


@router.get("/monitor/wells/{well_id}/external-data", response_model=ExternalWellData)
def external_data_for_well(well_id: str) -> ExternalWellData:
    source_rows = db.effective_source_statuses(well_id)
    payloads = db.load_source_payloads(well_id)
    crews = [
        item for payload in payloads.values()
        for item in payload.get("crew_availability", [])
    ][:5000]
    notes = [
        f"{row['source_id']}: {row['status']} — {row['message']}"
        for row in source_rows if row.get("message")
    ]
    return ExternalWellData(
        source_statuses=source_status_contract(source_rows),
        crew_availability=crews,
        notes=notes,
    )


@router.post("/monitor/run", response_model=MonitoringResult,
             responses={202: {"description": "Background job accepted"},
                        409: {"description": "Another monitor run is in progress"}})
def run_scheduled_monitor(
    request_data: MonitorRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    background: bool = Query(default=False, description=(
        "Run as a background job and return 202 with job_id; poll /monitor/jobs/{job_id}. "
        "Without it the call blocks until the run completes (n8n-compatible).")),
):
    if background:
        job_id = str(uuid.uuid4())
        job = db.create_monitor_job(job_id, request_data.horizon_days)
        background_tasks.add_task(run_monitor_job, job_id, request_data.horizon_days, request.app.state)
        return JSONResponse(status_code=202, content={
            **job, "status_url": f"/api/v1/monitor/jobs/{job_id}",
        })
    try:
        return run_monitor(request_data.horizon_days, request.app.state,
                           getattr(request.state, "request_id", None))
    except MonitorBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/monitor/jobs/{job_id}")
def get_monitor_job(job_id: str) -> dict:
    job = db.get_monitor_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Monitor job was not found")
    return job
