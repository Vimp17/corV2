"""Append-only CO₂ results with deterministic identities and transactional imports."""
from datetime import datetime, timezone
from sqlalchemy import select
from app.core.co2_corrosion import digest
from app.db.base import engine, _merge, _queue_writes, _retry_deadlocks
from app.db.schema import (co2_assessments, well_rating, telemetry, ingestion_batches,
                           source_documents, document_contributions)


def automatic_inputs(well_ids) -> dict[str, dict]:
    """Three reads per batch, reusing the card's source merge; automatic reads never write history."""
    from app.db.sources import _combine_telemetry
    ids = sorted(set(well_ids))
    if not ids:
        return {}
    data = {well: {'versions': [], 'payloads': {}} for well in ids}
    with engine().connect() as conn:
        for offset in range(0, len(ids), 1000):
            batch = ids[offset:offset+1000]
            query = (select(telemetry.c.well_id, telemetry.c.payload_json, ingestion_batches.c.created_at)
                .join(ingestion_batches, telemetry.c.batch_id == ingestion_batches.c.batch_id)
                .where(telemetry.c.well_id.in_(batch)).order_by(telemetry.c.id))
            for well, payload, updated in conn.execute(query):
                data[well]['versions'].append((payload, updated))
            for well, source, payload in conn.execute(select(source_documents.c.well_id,
                    source_documents.c.source_id, source_documents.c.payload_json)
                    .where(source_documents.c.well_id.in_(batch))):
                data[well]['payloads'][source] = payload
            for row in conn.execute(select(document_contributions).where(document_contributions.c.well_id.in_(batch))
                    .order_by(document_contributions.c.updated_at, document_contributions.c.document_id)).mappings():
                item = data[row['well_id']]
                item['versions'].extend((record, row['updated_at']) for record in row['data'].get('records', []))
                if row['data'].get('payload'):
                    item['payloads'][row['source_id']] = row['data']['payload']
    return {well: {'records': _combine_telemetry(item['versions']), 'payloads': item['payloads']}
            for well, item in data.items()}


def _known(conn, ids: set[str]) -> set[str]:
    ids = sorted(ids)
    return {well for offset in range(0, len(ids), 1000) for well in conn.execute(
        select(well_rating.c.well_id).where(well_rating.c.well_id.in_(ids[offset:offset+1000]))).scalars()}


def known_wells(ids: set[str]) -> set[str]:
    with engine().connect() as conn:
        return _known(conn, ids)


@_retry_deadlocks
def save_assessments(assessments: list[dict], create_wells: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    rows = []
    for a in assessments:
        identity = digest({"well": a["well_id"], "version": a["model_version"], "input": a["input_hash"],
                           "source": a.get("source_hash"), "row": a.get("source_row"), "synthetic": a["synthetic"]})
        rows.append({"assessment_id": identity, "well_id": a["well_id"],
            "state_at": datetime.fromisoformat(a["state_at"]) if a.get("state_at") else None,
            "created_at": now, "model_version": a["model_version"],
            "result_json": {**a, "assessment_id": identity, "created_at": now.isoformat()}})
    with engine().begin() as conn:
        _queue_writes(conn, "co2_assessments")
        unknown = {a["well_id"] for a in assessments} - _known(conn, {a["well_id"] for a in assessments})
        if unknown and not create_wells:
            raise ValueError("Неизвестные скважины; подтвердите создание: " + ", ".join(sorted(unknown)[:20]))
        created = _merge(conn, well_rating, [{"well_id": well, "source": "co2", "updated_at": now,
            "assessed_at": now} for well in sorted(unknown)], ("well_id",), output="well_id")
        inserted = _merge(conn, co2_assessments, rows, ("assessment_id",), output="assessment_id")
    return {"stored": len(inserted), "duplicates": len(assessments)-len(inserted), "created_wells": len(created),
            "assessment_ids": [row["assessment_id"] for row in rows]}


def history(well_id: str, limit: int = 20) -> list[dict]:
    with engine().connect() as conn:
        return list(conn.execute(select(co2_assessments.c.result_json).where(co2_assessments.c.well_id == well_id)
            .order_by(co2_assessments.c.created_at.desc(), co2_assessments.c.assessment_id).limit(limit)).scalars())


def by_id(assessment_id: str) -> dict | None:
    with engine().connect() as conn:
        return conn.execute(select(co2_assessments.c.result_json).where(
            co2_assessments.c.assessment_id == assessment_id)).scalar()
