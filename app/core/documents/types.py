"""Document types from config/document_types.json (MAI_DOCS_TYPES_PATH)."""
from __future__ import annotations

import json
from functools import lru_cache

from app.config import settings

TARGETS = {"work_event", "telemetry", "passport"}
FIELD_TYPES = {"string", "date", "number", "boolean"}
# Fields every target needs to place a record: the well and the date of the event / sample.
DATE_FIELD = {"work_event": "act_date", "telemetry": "sample_date", "passport": "passport_date"}


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
    try:
        return {spec["source_id"]: f"{spec.get('title') or spec['source_id']} (документ)"
                for spec in load_document_types().values()}
    except DocumentTypesError:
        return {}


def lab_source_ids() -> set[str]:
    try:
        return {spec["source_id"] for spec in load_document_types().values() if spec.get("lab_samples")}
    except DocumentTypesError:
        return set()


def passport_source_ids() -> set[str]:
    try:
        return {spec["source_id"] for spec in load_document_types().values() if spec["target"] == "passport"}
    except DocumentTypesError:
        return set()
