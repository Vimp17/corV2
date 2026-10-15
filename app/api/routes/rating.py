"""Well rating computed from source data, its Excel export, and ERA repair proposals.

The rating is recomputed by the daily cycle (POST /monitor/run, which pulls configured source
APIs first), by the per-well analysis (POST /monitor/wells/{id}/analyze) and after uploads or
pushes of DARS, ХИМИК and ERA data. It is read-only here apart from the operator fields.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app import db
from app.config import settings
from app.core import promine, rating, rating_export
from app.core.excel_ingestion import ExcelImportError
from app.core.source_connectors import source_modes
from app.schemas import EraProposalRequest, RatingWellPatch

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rating"])


def _rated_wells() -> list[dict]:
    wells = rating.build_rating(db.list_rating_rows())
    proposals = db.latest_era_proposals()
    for well in wells:
        well["last_proposal"] = proposals.get(well["well_id"])
    return wells


def _data_state(wells: list[dict]) -> dict:
    """What the rating is built from, so the dashboard can explain an empty or partial rating."""
    known = [row["well_id"] for row in db.list_wells()]
    rated = {well["well_id"] for well in wells if well.get("computed_at")}
    try:
        sources = source_modes()
    except ExcelImportError:
        logger.exception("Source profiles could not be loaded for the rating state")
        sources = []
    computed = [well["computed_at"] for well in wells if well.get("computed_at")]
    return {
        "known_wells": len(known),
        "computed_wells": len(rated),
        "pending_wells": sorted(set(known) - rated)[:500],
        "last_computed_at": max(computed) if computed else None,
        "sources": sources,
        "api_configured": any(item["mode"] in {"api", "adapter"} for item in sources),
    }


def _payload() -> dict:
    wells = _rated_wells()
    ready, explanation = promine.configured()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "combine": settings.rating_combine,
            "horizon_days": settings.rating_horizon_days,
            "protection": {"mode": settings.rating_protection_mode,
                           "factor": settings.rating_protection_factor,
                           "min_inhibitor": settings.rating_protection_min_inhibitor,
                           "max_injection_dev": settings.rating_protection_max_injection_dev,
                           "max_rate": settings.rating_protection_max_rate,
                           "labels": rating.PROTECTION_LABELS},
            "thresholds": rating.thresholds(),
            "measures": rating.measures_catalog(),
            "promine": {"transport": settings.promine_transport, "ready": ready, "message": explanation},
            "change_types": [{"key": key, "label": label} for key, label in promine.CHANGE_LABELS.items()],
        },
        "counts": rating.summarize(wells),
        "data": _data_state(wells),
        "wells": wells,
    }


@router.get("/rating")
def get_rating() -> dict:
    return _payload()


@router.get("/rating/export.xlsx", response_class=Response,
            responses={200: {"content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}}}})
def export_rating() -> Response:
    """The current rating as an Excel workbook in the approved layout (values, not formulas)."""
    content = rating_export.build_workbook(_payload())
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(content=content,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="rating_{stamp}.xlsx"',
                             "Cache-Control": "no-store"})


@router.get("/rating/wells/{well_id}")
def get_rating_well(well_id: str) -> dict:
    well = next((item for item in _rated_wells() if item["well_id"] == well_id), None)
    if well is None:
        raise HTTPException(status_code=404, detail="Скважины нет в рейтинге")
    return {**well, "proposals": db.list_era_proposals(well_id, limit=20)}


@router.patch("/rating/wells/{well_id}")
def patch_rating_well(well_id: str, request: RatingWellPatch) -> dict:
    """Operator fields only: plan date (ППР/ДДО), Промайн document status, comment."""
    values = request.model_dump(include=request.model_fields_set)
    if not values:
        raise HTTPException(status_code=422, detail="Нет полей для изменения")
    if not db.update_rating_row(well_id, values):
        raise HTTPException(status_code=404, detail="Скважины нет в рейтинге")
    return get_rating_well(well_id)


@router.get("/promine/era-proposals")
def list_proposals(well_id: str | None = Query(default=None, max_length=128),
                   limit: int = Query(default=50, ge=1, le=500)) -> dict:
    return {"proposals": db.list_era_proposals(well_id, limit)}


@router.post("/promine/era-proposals", status_code=201)
def create_proposal(request: EraProposalRequest) -> dict:
    """Store a proposal to change the ERA repair plan and send it through Промайн.

    The proposal is kept whatever happens on delivery; its status says whether it was sent
    (sent), could not be sent (failed) or waits for the transport to be configured.
    """
    well = next((item for item in _rated_wells() if item["well_id"] == request.well_id), None)
    if well is None:
        raise HTTPException(status_code=404, detail="Скважины нет в рейтинге")
    return promine.create_and_send(well, request.change_type, request.reason,
                                   request.proposed_date, request.author)


@router.post("/promine/era-proposals/{proposal_id}/resend")
def resend_proposal(proposal_id: str) -> dict:
    proposal = db.get_era_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Предложение не найдено")
    if proposal["status"] == "sent":
        raise HTTPException(status_code=409, detail="Предложение уже отправлено")
    return promine.deliver(proposal_id)
