"""CO₂ demo: strict import, reproducible calculation and history."""
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from app.config import settings
from app.core import co2_corrosion, co2_import, co2_inputs
from app.core.norsok import ModelConfigurationError, load_model
from app.db import co2
from app.schemas import CO2Calculation


def enabled():
    if not settings.co2_enabled:
        raise HTTPException(404, "Функция CO₂ отключена (MAI_CO2_ENABLED).")


router = APIRouter(prefix="/co2", tags=["co2"], dependencies=[Depends(enabled)])


@router.post("/calculate")
def calculate(request: CO2Calculation):
    try:
        result = co2_corrosion.calculate(request)
    except ModelConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        saved = co2.save_assessments([result], request.create_well)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"assessment": co2.by_id(saved["assessment_ids"][0]), **saved}


def current_model():
    try:
        return load_model()
    except ModelConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get('/model')
def model_info():
    model = current_model()
    return {key: model[key] for key in ('model_id', 'revision', 'title', 'algorithm', 'source',
                                       'configuration_hash', 'domain', 'rate_multiplier')}


@router.get('/input-template')
def input_template():
    return Response(co2_inputs.template_csv(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="norsok-inputs.csv"'})


@router.post('/calculate-file')
def calculate_file(file: UploadFile = File(...), confirm: bool = Form(False),
                   create_wells: bool = Form(False), synthetic: bool = Form(False),
                   configuration_hash: str = Form('')):
    model = current_model()
    if confirm and configuration_hash != model['configuration_hash']:
        raise HTTPException(409, 'Методика изменилась либо не проверена. Повторите проверку файла перед записью.')
    content = file.file.read(settings.max_excel_bytes + 1)
    if len(content) > settings.max_excel_bytes:
        raise HTTPException(413, 'Файл превышает предел размера.')
    try:
        report = co2_inputs.parse(content, file.filename or '', synthetic, model)
    except ModelConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except (ValueError, TypeError, KeyError, OSError, BadZipFile, ParseError, RecursionError) as exc:
        raise HTTPException(422, f'Исходные данные NORSOK: {exc}') from exc
    ids = {a['well_id'] for a in report['assessments']}
    warnings = [{'row': a['source_row'], 'well_id': a['well_id'], 'message': flag}
                for a in report['assessments'] for flag in a['quality_flags']]
    response = {k: v for k, v in report.items() if k != 'assessments'}
    response.update(unknown_wells=sorted(ids - co2.known_wells(ids)), confirmed=False, synthetic=synthetic,
                    warning_count=len(warnings), warnings=warnings[:100],
                    preview=[{'well_id': a['well_id'], 'row': a['source_row'], 'scenarios': a['scenarios']}
                             for a in report['assessments'][:20]],
                    verification='Рассчитано по исходным данным NORSOK M-506:2005. Проверьте ограничения и единицы перед записью.')
    if confirm:
        if report['errors']:
            raise HTTPException(422, 'Есть ошибки строк; файл не записан. Исправьте все строки и повторите проверку.')
        try:
            response.update(co2.save_assessments(report['assessments'], create_wells), confirmed=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return response


@router.get("/wells/{well_id}")
def history(well_id: str, limit: int = Query(20, ge=1, le=100)):
    return {"well_id": well_id, "assessments": co2.history(well_id, limit)}


@router.post("/import")
def import_v4(file: UploadFile = File(...), confirm: bool = Form(False),
                    create_wells: bool = Form(False), synthetic: bool = Form(True)):
    content = file.file.read(settings.max_excel_bytes + 1)
    if len(content) > settings.max_excel_bytes:
        raise HTTPException(413, "Файл превышает предел размера.")
    try:
        report = co2_import.parse(content, file.filename or "", synthetic)
    except (ValueError, TypeError, KeyError, OSError, BadZipFile, ParseError, RecursionError) as exc:
        raise HTTPException(422, f"Формат CO₂ v4: {exc}") from exc
    ids = {a["well_id"] for a in report["assessments"]}
    unknown = sorted(ids - co2.known_wells(ids))
    response = {k: v for k, v in report.items() if k != "assessments"}
    warnings = [{"row": a.get("source_row"), "well_id": a["well_id"], "message": flag}
                for a in report["assessments"] for flag in a["quality_flags"]]
    response.update(warning_count=len(warnings), warnings=warnings[:100],
                    verification="Проверен формат и доступные зависимости v4. Это не полевая валидация модели.")
    response.update(unknown_wells=unknown, synthetic=synthetic, confirmed=False)
    if confirm:
        if report["errors"]:
            raise HTTPException(422, "Есть ошибки строк; исправьте файл перед импортом.")
        try:
            response.update(co2.save_assessments(report["assessments"], create_wells), confirmed=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return response
