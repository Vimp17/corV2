from fastapi import APIRouter, File, HTTPException, Query, UploadFile, Form

from app import db
from app.config import settings
from app.core.excel_ingestion import ExcelImportError, load_profiles
from app.core.source_connectors import ingest_excel_bytes
from app.schemas import HistoryIngestionRequest, IngestionRequest

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.post("/telemetry", status_code=201)
def ingest_telemetry(request: IngestionRequest) -> dict:
    try:
        profiles = load_profiles()
        profile = next((item for item in profiles.values()
                        if str(item.get("source_id", "")).upper() == request.source_id), None)
        if profile is None or profile.get("kind") != "telemetry":
            raise HTTPException(status_code=422, detail="source_id must name a configured telemetry source")
        records = [record.model_copy(update={"source_id": request.source_id}) for record in request.records]
        result = db.save_batch(request.well_id, request.batch_id, records)
        status_row = db.record_source_status(request.source_id, request.well_id, "ok", None,
                                             len(records), origin="api_push")
        return {**result, "well_id": request.well_id, "source_id": request.source_id,
                "rows_accepted": len(records), "source_statuses": [status_row]}
    except ExcelImportError as exc:
        raise HTTPException(status_code=500, detail="Source profiles could not be loaded") from exc
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/wells")
def get_wells() -> list[dict]:
    return db.list_wells()


@router.get("/sources")
def get_sources() -> dict:
    try:
        profiles = load_profiles()
    except ExcelImportError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "sources": [{"profile_id": profile_id,
                     "source_id": str(profile.get("source_id", profile_id)).upper(),
                     "kind": profile.get("kind"),
                     "required_columns": profile.get("required_columns", []),
                     "mapping_columns": sorted(profile.get("field_map", {}))}
                    for profile_id, profile in profiles.items()],
        "last_status": db.list_source_statuses(),
        "excel_upload": {"format": "xlsx", "max_bytes": settings.max_excel_bytes,
                         "max_rows": settings.max_excel_rows},
    }


@router.post("/excel", status_code=201)
async def ingest_excel(file: UploadFile = File(...), profile_id: str = Form(...),
                       well_id: str | None = Form(default=None)) -> dict:
    filename = file.filename or "upload.xlsx"
    content = await file.read(settings.max_excel_bytes + 1)
    await file.close()
    if len(content) > settings.max_excel_bytes:
        raise HTTPException(status_code=413, detail="Workbook exceeds the configured upload size limit")
    try:
        return ingest_excel_bytes(content, filename, profile_id, well_id)
    except ExcelImportError as exc:
        if well_id:
            try:
                profile = load_profiles().get(profile_id, {})
                source_id = str(profile.get("source_id", profile_id)).upper()
                db.record_source_status(source_id, well_id, "error", str(exc)[:2000], 0)
            except Exception:
                pass
        raise HTTPException(status_code=422, detail={
            "message": str(exc),
            "next_step": "Check the worksheet/header row and update config/source_profiles.json aliases.",
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.post("/crew-schedule", status_code=201)
def ingest_crew_schedule(
    payload: dict,
    well_id: str | None = Query(default=None, max_length=128),
    all_wells: bool = Query(default=False),
) -> dict:
    """Accept crew availability JSON and store it per well or for all known wells."""
    from app.schemas import CrewAvailability

    source_id = "CREW_SCHEDULE"

    crew_rows = (
        payload.get("crew_availability")
        if isinstance(payload.get("crew_availability"), list)
        else payload.get("records")
        if isinstance(payload.get("records"), list)
        else None
    )

    if crew_rows is None:
        raise HTTPException(
            status_code=422,
            detail="JSON must contain a 'crew_availability' array.",
        )
    if not crew_rows:
        raise HTTPException(status_code=422, detail="crew_availability array is empty")
    if len(crew_rows) > 5000:
        raise HTTPException(status_code=422, detail="Maximum 5000 crew records per request")

    canonical_rows = []
    errors = []
    for index, row in enumerate(crew_rows):
        if not isinstance(row, dict):
            errors.append({"row": index + 1, "message": "Each item must be a JSON object"})
            continue
        try:
            item = CrewAvailability.model_validate({**row, "source_id": source_id})
            canonical_rows.append(item.model_dump(mode="json"))
        except Exception as exc:
            errors.append({"row": index + 1, "message": str(exc)[:500]})

    if not canonical_rows:
        raise HTTPException(
            status_code=422,
            detail={"message": "No valid crew records", "errors": errors[:20]},
        )

    if all_wells:
        targets = [row["well_id"] for row in db.list_wells()]
    elif well_id and well_id.strip():
        targets = [well_id.strip()]
    else:
        raise HTTPException(status_code=422, detail="Specify well_id or all_wells=true")

    if not targets:
        raise HTTPException(
            status_code=422,
            detail="No wells known yet; upload telemetry first or specify well_id",
        )

    status = "ok" if not errors else "partial"
    message = f"{len(canonical_rows)} crew records accepted, {len(errors)} rejected."
    if errors:
        message += " Examples: " + "; ".join(
            f"row {e['row']}: {e['message']}" for e in errors[:3]
        )
    message = message[:2000]

    statuses = []
    for target in targets:
        db.save_source_payload(source_id, target, {"crew_availability": canonical_rows})
        statuses.append(
            db.record_source_status(
                source_id, target, status, message, len(canonical_rows),
                origin="manual_upload",
            )
        )

    return {
        "status": status,
        "source_id": source_id,
        "targets": targets,
        "rows_accepted": len(canonical_rows),
        "rows_rejected": len(errors),
        "errors": errors[:20],
        "source_statuses": statuses,
        "message": message,
    }


@router.get("/wells/{well_id}/telemetry")
def get_telemetry(well_id: str) -> dict:
    records = db.load_telemetry(well_id)
    if not records:
        raise HTTPException(status_code=404, detail="No telemetry found for well_id")
    return {"well_id": well_id, "records": records}


@router.post("/history", status_code=201)
def ingest_history(request: HistoryIngestionRequest) -> dict:
    try:
        profiles = load_profiles()
        profile = next((item for item in profiles.values()
                        if str(item.get("source_id", "")).upper() == request.source_id), None)
        if profile is None or profile.get("kind") != "history":
            raise HTTPException(status_code=422, detail="source_id must name a configured history source")
        result = db.save_history(request.well_id, request.batch_id,
                                 request.work_history, request.failure_history,
                                 source_id=request.source_id)
        status_row = db.record_source_status(request.source_id, request.well_id, "ok", None,
                                             len(request.work_history) + len(request.failure_history),
                                             origin="api_push")
        return {**result, "well_id": request.well_id, "source_id": request.source_id,
                "rows_accepted": len(request.work_history) + len(request.failure_history),
                "source_statuses": [status_row]}
    except ExcelImportError as exc:
        raise HTTPException(status_code=500, detail="Source profiles could not be loaded") from exc
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/wells/{well_id}/history")
def get_history(well_id: str) -> dict:
    return {"well_id": well_id, **db.load_history(well_id)}
