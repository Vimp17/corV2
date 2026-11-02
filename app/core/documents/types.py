"""Document types from config/document_types.json (MAI_DOCS_TYPES_PATH).

Every type also looks for the fields of the well card (CARD_FIELDS) that it does not define itself:
whatever kind of document it is, a tonnage, a liquid rate or a run date written in it reaches the
well card (app.core.documents.load). A type marked "fallback" takes the documents no other type
matches: they load the card fields they hold.
"""
from __future__ import annotations

import json
from functools import lru_cache

from app.config import settings

TARGETS = {"work_event", "telemetry", "passport"}
# "measurements": a list of other indicators [{name, value, quote}] (pH, hardness…).
FIELD_TYPES = {"string", "date", "number", "boolean", "measurements"}
# Fields every target needs to place a record: the well and the date of the event / sample.
DATE_FIELD = {"work_event": "act_date", "telemetry": "sample_date", "passport": "passport_date"}
# Fields of the well card any document may hold. Measurements go to a well data row dated by the
# document; the equipment with its run date becomes an equipment-run event (the run time).
CARD_FIELDS: dict[str, dict] = {
    "field": {"label": "Месторождение", "type": "string"},
    "water_cut_pct": {"label": "Обводнённость, %", "type": "number"},
    "co2_pct": {"label": "CO₂, %", "type": "number", "hint": "Только если указано в процентах."},
    "chlorides_mg_l": {"label": "Хлориды, мг/л", "type": "number", "hint": "Только если указано в мг/л."},
    "inhibitor_efficiency": {"label": "Эффективность ингибитора, доля 0–1", "type": "number",
                             "hint": "Если указана в процентах — разделите на 100."},
    "injection_deviation_pct": {"label": "Отклонение закачки (дозировки) ингибитора, %", "type": "number"},
    "corrosion_rate_mm_year": {"label": "Скорость коррозии, мм/год", "type": "number",
                               "hint": "Только если указана в мм/год."},
    "wall_thickness_mm": {"label": "Толщина стенки НКТ остаточная, мм", "type": "number"},
    "initial_wall_thickness_mm": {"label": "Толщина стенки НКТ начальная (номинальная), мм", "type": "number"},
    "liquid_rate_m3_day": {"label": "Дебит жидкости, м³/сут", "type": "number"},
    "tonnage_t": {"label": "Тоннаж обработки (реагента, ингибитора), т", "type": "number"},
    "noo_days": {"label": "НОО, сут", "type": "number", "hint": "Как в документе: «НОО», сут."},
    "equipment": {"label": "Спущенное оборудование", "type": "string",
                  "hint": "Тип насоса и НКТ, исполнение (с покрытием, коррозионностойкое), контейнер ингибитора."},
    "install_date": {"label": "Дата спуска оборудования", "type": "date"},
    "extra_measurements": {"label": "Другие показатели", "type": "measurements", "hint": (
        "Числовые показатели скважины или пробы, которых нет среди полей выше (например pH, жёсткость, "
        "железо, сульфаты, плотность, давление): название с единицей измерения, значение и цитата для каждого.")},
}
# Card fields that make a document worth loading on their own (a place name alone does not).
CARD_INFORMATIVE = tuple(name for name in CARD_FIELDS if name not in {"field", "equipment"})


class DocumentTypesError(ValueError):
    pass


def _validate(types: dict) -> dict:
    if not isinstance(types, dict) or not types:
        raise DocumentTypesError("document_types must be a non-empty object")
    source_ids: set[str] = set()
    for type_id, spec in types.items():
        where = f"document type '{type_id}'"
        if not isinstance(spec, dict):
            raise DocumentTypesError(f"{where} must be an object")
        if spec.get("target") not in TARGETS:
            raise DocumentTypesError(f"{where}: target must be one of {sorted(TARGETS)}")
        source_id = str(spec.get("source_id") or "").strip().upper()
        if not source_id or len(source_id) > 128 or source_id in source_ids:
            raise DocumentTypesError(f"{where}: source_id must be set and unique")
        source_ids.add(source_id)
        spec["source_id"] = source_id
        fields = spec.get("fields")
        if not isinstance(fields, dict) or "well_id" not in fields:
            raise DocumentTypesError(f"{where}: fields must include well_id")
        # The type's own fields first (its labels and hints win), then the rest of the card.
        fields = spec["fields"] = {**fields, **{name: dict(field) for name, field in CARD_FIELDS.items()
                                                if name not in fields}}
        date_field = DATE_FIELD[spec["target"]]
        if spec["target"] != "passport" and date_field not in fields:
            raise DocumentTypesError(f"{where}: fields must include {date_field}")
        for name, field in fields.items():
            if not isinstance(field, dict) or field.get("type") not in FIELD_TYPES:
                raise DocumentTypesError(f"{where}: field '{name}' must have a type in {sorted(FIELD_TYPES)}")
        if spec["target"] == "work_event" and not str(spec.get("work_type") or "").strip():
            raise DocumentTypesError(f"{where}: work_event types need work_type")
        if not isinstance(spec.get("keywords", []), list):
            raise DocumentTypesError(f"{where}: keywords must be a list")
        unknown = set(spec.get("informative_fields", [])) - set(fields)
        if unknown:
            raise DocumentTypesError(f"{where}: informative_fields not in fields: {sorted(unknown)}")
        spec["informative_fields"] = list(dict.fromkeys([*spec.get("informative_fields", []), *CARD_INFORMATIVE]))
    if sum(1 for spec in types.values() if spec.get("fallback")) > 1:
        raise DocumentTypesError("only one document type may be the fallback")
    return types


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict:
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except FileNotFoundError as exc:
        raise DocumentTypesError(f"Document types file is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DocumentTypesError(f"Document types file is invalid: {exc}") from exc
    return _validate(data.get("document_types"))


def load_document_types() -> dict:
    path = settings.docs_types_path
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError as exc:
        raise DocumentTypesError(f"Document types file is missing: {path}") from exc
    return _load(str(path), mtime)


def source_titles() -> dict[str, str]:
    """source_id -> title, for the rating provenance ("Акт разбора на базе (документ)")."""
    from app.core.documents import nkt_registry

    titles = {nkt_registry.SOURCE_ID: f"{nkt_registry.TITLE} (документ)"}
    try:
        return titles | {spec["source_id"]: f"{spec.get('title') or spec['source_id']} (документ)"
                         for spec in load_document_types().values()}
    except DocumentTypesError:
        return titles


def lab_source_ids() -> set[str]:
    try:
        return {spec["source_id"] for spec in load_document_types().values() if spec.get("lab_samples")}
    except DocumentTypesError:
        return set()

