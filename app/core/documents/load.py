"""Writing accepted document records into the usual source data.

  work_event (acts)       -> an ERA-like work event with the corrosion finding, so the rating
                             sees the demount / base-inspection act exactly as from ERA;
  telemetry (lab protocol) -> a laboratory sample row (counts as a ХИМИК-like sample);
  passport                 -> the passport fields per well (source_documents) and, when the
                             equipment and its run date are given, an equipment-run event.
Rows are stored under batch ids "doc-<document_id>-<well_id>", so reprocessing or deleting a
document removes exactly what it loaded.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timezone

from app import db
from app.core.documents.extract import values_of
from app.schemas import TelemetryPoint, WorkHistoryItem

LAB_FIELDS = ("water_cut_pct", "chlorides_mg_l", "co2_pct", "corrosion_rate_mm_year", "inhibitor_efficiency",
              "wall_thickness_mm", "initial_wall_thickness_mm")
INSTALL_WORK_TYPE = "Спуск оборудования (паспорт скважины)"


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


def load_records(document: dict, spec: dict, records: list[dict]) -> dict:
    """Store accepted records; returns {"wells": [...], "records": n, "by_well": {...}}."""
    db.delete_document_data(document["document_id"])
    source_id = spec["source_id"]
    accepted = [record for record in records if record["accepted"] and record["well_id"]]
    by_well: dict[str, list[dict]] = defaultdict(list)
    for record in accepted:
        by_well[record["well_id"]].append(values_of(record))
    summary: dict[str, dict] = {}
    for well_id, items in by_well.items():
        if spec["target"] == "work_event":
            work = [_work_item(values, spec) for values in items]
            db.save_history(well_id, batch_id(document["document_id"], well_id), work, [], source_id=source_id)
            count = len(work)
        elif spec["target"] == "telemetry":
            points = []
            for values in items:
                payload = {field: values[field] for field in LAB_FIELDS if values.get(field) is not None}
                points.append(TelemetryPoint.model_validate({
                    "timestamp": _moment(values["sample_date"]), "source_id": source_id, **payload}))
            db.save_batch(well_id, batch_id(document["document_id"], well_id), points)
            count = len(points)
        else:  # passport: the latest passport of the well wins
            values = items[-1]
            db.save_source_payload(source_id, well_id, {
                "document_id": document["document_id"], "filename": document["filename"],
                "passport": values})
            if values.get("equipment") and values.get("install_date"):
                db.save_history(well_id, batch_id(document["document_id"], well_id), [WorkHistoryItem(
                    date=_moment(values["install_date"]), work_type=INSTALL_WORK_TYPE,
                    equipment=str(values["equipment"])[:256])], [], source_id=source_id)
            count = 1
        db.record_source_status(source_id, well_id, "ok",
                                f"{spec.get('title')}: «{document['filename']}»", count, origin="document")
        summary[well_id] = {"records": count}
    return {"wells": sorted(summary), "records": sum(item["records"] for item in summary.values()),
            "by_well": summary, "source_id": source_id}
