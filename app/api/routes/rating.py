"""Well rating (approved template) and ERA repair proposals sent through Промайн."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from app import db
from app.config import settings
from app.core import promine, rating
from app.schemas import EraProposalRequest, RatingUpsertRequest, RatingWellPatch

router = APIRouter(tags=["rating"])


def _rated_wells() -> list[dict]:
    wells = rating.build_rating(db.list_rating_rows())
    proposals = db.latest_era_proposals()
    for well in wells:
        well["last_proposal"] = proposals.get(well["well_id"])
    return wells


@router.get("/rating")
def get_rating() -> dict:
    wells = _rated_wells()
    ready, explanation = promine.configured()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "combine": settings.rating_combine,
            "thresholds": rating.thresholds(),
            "measures": rating.measures_catalog(),
            "promine": {"transport": settings.promine_transport, "ready": ready, "message": explanation},
            "change_types": [{"key": key, "label": label} for key, label in promine.CHANGE_LABELS.items()],
        },
        "counts": rating.summarize(wells),
        "wells": wells,
    }


@router.get("/rating/wells/{well_id}")
def get_rating_well(well_id: str) -> dict:
    well = next((item for item in _rated_wells() if item["well_id"] == well_id), None)
    if well is None:
        raise HTTPException(status_code=404, detail="Скважины нет в рейтинге")
    return {**well, "proposals": db.list_era_proposals(well_id, limit=20)}


@router.post("/rating/wells", status_code=201)
def upsert_rating_wells(request: RatingUpsertRequest) -> dict:
    """Push rating rows from another system (JSON counterpart of the Excel template)."""
    records = [item.model_dump(include=item.model_fields_set | {"well_id"}) for item in request.wells]
    result = db.upsert_rating(records, source="api_push")
    return {**result, "rows_accepted": len(records)}


@router.patch("/rating/wells/{well_id}")
def patch_rating_well(well_id: str, request: RatingWellPatch) -> dict:
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
