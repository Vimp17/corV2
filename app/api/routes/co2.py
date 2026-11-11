"""CO₂ demo: strict import, reproducible calculation and history."""
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from app.config import settings
from app.core import co2_corrosion, co2_import
from app.db import co2
from app.db.rating import get_rating_row
from app.schemas import CO2Calculation


def enabled():
    if not settings.co2_enabled:
        raise HTTPException(404, "Функция CO₂ отключена (MAI_CO2_ENABLED).")


router = APIRouter(prefix="/co2", tags=["co2"], dependencies=[Depends(enabled)])


@router.post("/calculate")
def calculate(request: CO2Calculation):
    if get_rating_row(request.well_id) is None and not request.create_well:
        raise HTTPException(409, "Неизвестная скважина; подтвердите создание (create_well).")
    result = co2_corrosion.calculate(request)
    try:
        saved = co2.save_assessments([result], request.create_well)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"assessment": co2.by_id(saved["assessment_ids"][0]), **saved}


@router.get("/wells/{well_id}")
def history(well_id: str, limit: int = Query(20, ge=1, le=100)):
    return {"well_id": well_id, "assessments": co2.history(well_id, limit)}


@router.post("/import")
async def import_v4(file: UploadFile = File(...), confirm: bool = Form(False),
                    create_wells: bool = Form(False), synthetic: bool = Form(True)):
    content = await file.read(settings.max_excel_bytes + 1)
    if len(content) > settings.max_excel_bytes:
        raise HTTPException(413, "Файл превышает предел размера.")
    try:
        report = co2_import.parse(content, file.filename or "", synthetic)
    except (ValueError, TypeError, KeyError, OSError, BadZipFile, ParseError) as exc:
        raise HTTPException(422, f"Формат CO₂ v4: {exc}") from exc
    from app import db
    known = {r["well_id"] for r in db.list_rating_rows()}
    unknown = sorted({a["well_id"] for a in report["assessments"]} - known)
    response = {k: v for k, v in report.items() if k != "assessments"}
    response.update(unknown_wells=unknown, synthetic=synthetic, confirmed=False)
    if confirm:
        if report["errors"]:
            raise HTTPException(422, "Есть ошибки строк; исправьте файл перед импортом.")
        try:
            response.update(co2.save_assessments(report["assessments"], create_wells), confirmed=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return response
