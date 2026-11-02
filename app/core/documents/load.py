"""Writing accepted document records into the usual source data.

  work_event (acts)       -> an ERA-like work event with the corrosion finding, so the rating
                             sees the demount / base-inspection act exactly as from ERA;
  telemetry (lab protocol) -> a laboratory sample row (counts as a ХИМИК-like sample);
  passport                 -> the passport fields per well (source_documents).
Whatever the type, the well card fields the document holds are loaded too (types.CARD_FIELDS): the
measurements (tonnage, liquid rate, environment, wall thickness, other indicators…) as a well data
row dated by the document, the equipment with its run date as an equipment-run event.
Rows are stored under batch ids "doc-<document_id>-<well_id>", so reprocessing or deleting a
document removes exactly what it loaded.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timezone

from app import db
from app.core.documents.extract import values_of
from app.core.documents.types import DATE_FIELD
from app.schemas import TelemetryPoint, WorkHistoryItem

# Measurement fields a document may carry (lab protocols; any Excel table of indicators).
LAB_FIELDS = ("water_cut_pct", "chlorides_mg_l", "co2_pct", "corrosion_rate_mm_year", "inhibitor_efficiency",
              "wall_thickness_mm", "initial_wall_thickness_mm", "injection_deviation_pct", "metal_loss_mm",
              "liquid_rate_m3_day", "tonnage_t", "noo_days", "field")
INSTALL_WORK_TYPE = "Спуск оборудования (документ)"


def batch_id(document_id: str, well_id: str) -> str:
    return f"doc-{document_id}-{well_id}"[:128]


def _moment(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), time(12, 0), tzinfo=timezone.utc)


def _join(*parts) -> str | None:
    text = "; ".join(str(part).strip() for part in parts if part not in (None, ""))
    return text[:2000] or None


def _work_item(values: dict, spec: dict) -> WorkHistoryItem:
    return WorkHistoryItem(
        date=_moment(values["act_date"]),
        work_type=str(spec["work_type"])[:128],
        reason=_join(values.get("cause")),
        equipment=(values.get("equipment") or None) and str(values["equipment"])[:256],
        result=_join(values.get("corrosion_description"), values.get("corrosion_type"),
                     values.get("corrosion_location") and f"место: {values['corrosion_location']}"),
        corrosion_detected=values.get("corrosion_detected"),
    )


def _point(values: dict, day: str, source_id: str) -> TelemetryPoint | None:
    """The document's measurements as one well data row on its date; None without any (a place name
    alone is no measurement: a row would make the data look fresh)."""
    payload = {field: values[field] for field in LAB_FIELDS if values.get(field) is not None}
    extras = {item["name"]: item["value"] for item in values.get("extra_measurements") or []}
    if extras:
        payload["extra_measurements"] = extras
    if set(payload) <= {"field"}:
        return None
    return TelemetryPoint.model_validate({"timestamp": _moment(day), "source_id": source_id, **payload})


def _install_item(values: dict) -> WorkHistoryItem | None:
    """The equipment and the date it was run in: an equipment-run event (the run time counts from it)."""
    if not (values.get("equipment") and values.get("install_date")):
        return None
    return WorkHistoryItem(date=_moment(values["install_date"]), work_type=INSTALL_WORK_TYPE,
                           equipment=str(values["equipment"])[:256])


def load_records(document: dict, spec: dict, records: list[dict]) -> dict:
    """Store accepted records; returns {"wells": [...], "records": n, "by_well": {...}}."""
    db.delete_document_data(document["document_id"])
    source_id = spec["source_id"]
    accepted = [record for record in records if record["accepted"] and record["well_id"]]
    by_well: dict[str, list[dict]] = defaultdict(list)
    for record in accepted:
        by_well[record["well_id"]].append(values_of(record))
    # A passport without a date: its measurements count from the day the file was received.
    received = date.today().isoformat()
    summary: dict[str, dict] = {}
    for well_id, items in by_well.items():
        work: list[WorkHistoryItem] = []
        points: list[TelemetryPoint] = []
        attributes: dict = {}
        for values in items:
            if spec["target"] == "work_event":
                work.append(_work_item(values, spec))
            install = _install_item(values)
            if install is not None:
                work.append(install)
            day = values.get(DATE_FIELD[spec["target"]]) or received
            point = _point(values, day, source_id)
            if point is not None:
                points.append(point)
            elif values.get("field"):
                attributes = {"field": values["field"], "passport_date": day}
        if spec["target"] == "passport":  # the latest passport of the well wins
            attributes = items[-1]
        if attributes:
            # Well attributes (the field) are read like a passport: app.core.rating_builder.well_passports.
            db.save_source_payload(source_id, well_id, {
                "document_id": document["document_id"], "filename": document["filename"], "passport": attributes})
        key = batch_id(document["document_id"], well_id)
        if work:
            db.save_history(well_id, key, work, [], source_id=source_id)
        if points:
            db.save_batch(well_id, key, points)
        count = len(items)
        db.record_source_status(source_id, well_id, "ok",
                                f"{spec.get('title')}: «{document['filename']}»", count, origin="document")
        summary[well_id] = {"records": count}
    return {"wells": sorted(summary), "records": sum(item["records"] for item in summary.values()),
            "by_well": summary, "source_id": source_id}
