from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from app import db
from app.config import settings
from app.core.orchestration import analyze_saved_well
from app.core.readiness import source_status_contract
from app.core.source_connectors import refresh_sources_for_well
from app.schemas import (
    DashboardSnapshot,
    ExternalWellData,
    MonitorRequest,
    MonitoringResult,
    PipelineResult,
    SkippedAnalysis,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["monitoring", "dashboard"])

_RISK_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "UNKNOWN": 4,
    "DATA_ISSUE": 5,
    "NOT_ANALYZED": 6,
}

_DASHBOARD_DATA_ISSUE_SOURCE_STATUSES = {"error", "unavailable"}
_SOURCE_FAILURE_STATUSES = {"error", "unavailable"}


def _probability_alert(result: PipelineResult) -> tuple[bool, float | None, str | None]:
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


def _is_alert(result: PipelineResult) -> tuple[bool, str]:
    risk_class = result.rule_risk.risk_class.upper()
    if risk_class in settings.alert_rule_classes:
        return True, f"Rule Engine: {risk_class} ({result.rule_risk.risk_points or 0} баллов)"
    probability_alert, probability, model_id = _probability_alert(result)
    if probability_alert:
        return True, f"{model_id}: калиброванная вероятность {probability:.1%} за {result.cox_risk.horizon_days} дней"
    return False, ""


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
        if _RISK_ORDER.get(risk_class, 99) > _RISK_ORDER["HIGH"]:
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
        "horizon_days": predictions[0].get("horizon_days") if predictions else None,  # ← ДОБАВИТЬ
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
    }


def _ranking() -> list[dict]:
    summaries = {row["well_id"]: row for row in db.list_wells()}
    
    # Получить ВСЕ анализы (не только последние)
    with db.engine().connect() as conn:
        all_runs = conn.execute(
            select(db.analysis_runs).order_by(db.analysis_runs.c.created_at.desc())
        ).mappings().all()
    
    source_map: dict[str, list[dict]] = {}
    for source in db.effective_source_statuses():
        source_map.setdefault(source["well_id"], []).append(source)
    
    rows = []
    seen_combinations = set()
    
    for run in all_runs:
        well_id = run["well_id"]
        analysis = run["result_json"] or {}
        predictions = analysis.get("model_predictions", [])
        horizon_days = predictions[0].get("horizon_days") if predictions else None
        
        # Уникальная комбинация: well_id + horizon_days
        combo_key = (well_id, horizon_days)
        if combo_key in seen_combinations:
            continue
        seen_combinations.add(combo_key)
        
        run_dict = {
            "analysis_id": run["analysis_id"],
            "well_id": well_id,
            "created_at": run["created_at"].isoformat(),
            "status": run["status"],
            "analysis": analysis,
            "agent_response": run["agent_response_json"],
            "agent_status": run["agent_status"],
            "agent_updated_at": run["agent_updated_at"].isoformat() if run["agent_updated_at"] else None,
        }
        
        rows.append(_dashboard_row(run_dict, summaries.get(well_id), source_map.get(well_id, [])))
    
    # Добавить скважины без анализа
    for well_id, summary in summaries.items():
        if well_id not in {r["well_id"] for r in rows}:
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
                "model_predictions": [],
                "ai_status": "pending",
                "ai_updated_at": None,
                "ai_report": None,
            })
    
    rows.sort(
        key=lambda row: (
            row.get("horizon_days") or 0,
            _RISK_ORDER.get(row["risk_class"], 99),
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
    return DashboardSnapshot(
        generated_at=datetime.now(timezone.utc),
        last_analysis_at=last_analysis_at,
        wells_total=len(ranking),
        at_risk_count=sum(
            row["risk_class"] in settings.alert_rule_classes for row in ranking
        ),
        data_issue_count=sum(
            row["risk_class"] == "DATA_ISSUE" or any(
                source["status"] in _DASHBOARD_DATA_ISSUE_SOURCE_STATUSES
                for source in row.get("source_statuses", [])
            )
            for row in ranking
        ),
        ranking=ranking,
    )


@router.post("/monitor/wells/{well_id}/analyze")
def analyze_one_well(well_id: str, request_data: MonitorRequest, request: Request):
    well_id = well_id.strip()
    if not well_id or len(well_id) > 128:
        raise HTTPException(status_code=422, detail="well_id must contain 1 to 128 characters")
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


@router.post("/monitor/run", response_model=MonitoringResult)
def run_scheduled_monitor(request_data: MonitorRequest, request: Request) -> MonitoringResult:
    started = datetime.now(timezone.utc)
    well_ids = list(dict.fromkeys([
        *settings.configured_well_ids,
        *(row["well_id"] for row in db.list_wells()),
    ]))
    alerts: list[dict] = []
    source_failures: list[dict] = []
    completed = skipped = 0
    
    # Keep dashboard horizons populated and include the horizon explicitly requested by the caller.
    horizons_for_monitor = sorted({30, 90, 180, request_data.horizon_days})
    
    for well_id in well_ids:
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
        
        for horizon_days in horizons_for_monitor:
            try:
                result = analyze_saved_well(
                    well_id,
                    horizon_days,
                    request.app.state,
                    getattr(request.state, "request_id", None),
                )
            except Exception as exc:
                logger.exception("Scheduled analysis failed for well_id=%s horizon=%d", well_id, horizon_days)
                source_failures.append({
                    "well_id": well_id,
                    "source_id": "analysis",
                    "status": "error",
                    "message": f"Analysis failed for horizon {horizon_days} ({type(exc).__name__}); inspect backend logs.",
                })
                continue
            
            if isinstance(result, SkippedAnalysis):
                skipped += 1
                for source in db.effective_source_statuses(well_id):
                    if source["status"] in _SOURCE_FAILURE_STATUSES:
                        source_failures.append({"well_id": well_id, **source})
                continue
            
            completed += 1
            is_risk, reason = _is_alert(result)
            if is_risk:
                alerts.append({
                    "well_id": well_id,
                    "analysis_id": result.analysis_id,
                    "risk_class": result.rule_risk.risk_class,
                    "risk_score": result.rule_risk.risk_score,
                    "risk_points": result.rule_risk.risk_points,
                    "horizon_days": horizon_days,
                    "reason": reason,
                    "dashboard_url": settings.dashboard_url,
                })
            
            for source in db.effective_source_statuses(well_id):
                if source["status"] in _SOURCE_FAILURE_STATUSES:
                    source_failures.append({"well_id": well_id, **source})
    
    completed_at = datetime.now(timezone.utc)
    
    if not well_ids:
        return MonitoringResult(
            status="needs_configuration",
            started_at=started,
            completed_at=completed_at,
            wells_checked=0,
            analyses_completed=0,
            analyses_skipped=0,
            alerts=[],
            source_failures=[],
            dashboard_url=settings.dashboard_url,
            message="Список скважин пуст. Укажите MAI_WELL_IDS или загрузите первые данные через API.",
        )
    
    return MonitoringResult(
        status="completed",
        started_at=started,
        completed_at=completed_at,
        wells_checked=len(well_ids),
        analyses_completed=completed,
        analyses_skipped=skipped,
        alerts=alerts,
        source_failures=source_failures,
        dashboard_url=settings.dashboard_url,
        message=(
            f"Проверено скважин: {len(well_ids)}. Высокий/критический риск: {len(alerts)}. "
            f"Пропущено из-за данных: {skipped}. Горизонты: {horizons_for_monitor}."
        ),
    )