"""Source data: telemetry, work and failure history, source statuses and payloads, the list of wells."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import delete, func, literal_column, select, union

from app.config import settings
from app.db.base import _merge, _queue_writes, _retry_deadlocks, engine
from app.db.schema import (_UNSPECIFIED_SOURCE, history_batches, history_events, ingestion_batches,
                           source_documents, source_health, telemetry, well_rating)
from app.schemas import FailureHistoryItem, TelemetryPoint, WorkHistoryItem


# --------------------------------------------------------------------------- source data
_SOURCE_ORIGINS = {"api_push", "document", "manual_upload", "api_refresh", "adapter", "system"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _json_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@_retry_deadlocks
def save_batch(well_id: str, requested_batch_id: str | None, records: Iterable[TelemetryPoint],
               replace: bool = False) -> dict:
    """Store telemetry; a value at a moment already stored for the source replaces the old one.
    A batch id used again with the same well and payload is an idempotent replay; with another
    payload it is an error, or — replace, a file imported again after its reading changed — the
    earlier rows of the batch give way to the new ones."""
    batch_id = requested_batch_id or str(uuid.uuid4())
    rows = list(records)
    persisted_fields = set(TelemetryPoint.model_fields) | {"source_id"}
    payloads = [{key: value for key, value in point.model_dump(mode="json", exclude_none=False).items()
                 if key in persisted_fields} for point in rows]
    occurrences: dict[tuple[str, str], int] = {}
    telemetry_rows = []
    for index, (point, payload) in enumerate(zip(rows, payloads)):
        source_id = payload.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 128:
            source_id = _UNSPECIFIED_SOURCE
            payload.pop("source_id", None)
        occurrence_key = (source_id, _utc(point.timestamp).isoformat())
        source_occurrence = occurrences.get(occurrence_key, 0)
        occurrences[occurrence_key] = source_occurrence + 1
        telemetry_rows.append({"batch_id": batch_id, "well_id": well_id, "source_id": source_id,
                               "source_occurrence": source_occurrence, "record_index": index,
                               "timestamp": point.timestamp, "payload_json": payload})
    digest = _json_hash(payloads)
    batch = {"batch_id": batch_id, "well_id": well_id, "record_count": len(rows),
             "created_at": datetime.now(timezone.utc), "payload_sha256": digest}
    with engine().begin() as conn:
        _queue_writes(conn, "telemetry")
        if not _merge(conn, ingestion_batches, [batch], ("batch_id",)):
            existing = conn.execute(select(ingestion_batches).where(
                ingestion_batches.c.batch_id == batch_id)).mappings().one()
            if existing["well_id"] == well_id and existing["payload_sha256"] == digest:
                return {"batch_id": batch_id, "well_id": well_id, "records_received": len(rows),
                        "records_stored": existing["record_count"], "idempotent_replay": True}
            if not replace:
                raise ValueError("batch_id has already been used with a different well or payload")
            conn.execute(delete(ingestion_batches).where(ingestion_batches.c.batch_id == batch_id))  # its rows too
            _merge(conn, ingestion_batches, [batch], ("batch_id",))
        if telemetry_rows:
            _merge(conn, telemetry, telemetry_rows, ("well_id", "source_id", "timestamp", "source_occurrence"),
                   update=("batch_id", "record_index", "payload_json"))
    return {"batch_id": batch_id, "well_id": well_id, "records_received": len(rows),
            "records_stored": len(rows), "idempotent_replay": False}


def load_telemetry(well_id: str) -> list[TelemetryPoint]:
    """The well's telemetry in time order; points of different sources at the same moment are merged
    into one point with source_id "A+B" (the first value of each field wins)."""
    with engine().connect() as conn:
        rows = conn.execute(select(telemetry.c.payload_json).where(
            telemetry.c.well_id == well_id).order_by(telemetry.c.timestamp, telemetry.c.id)).all()
    combined: list[TelemetryPoint] = []
    for point in (TelemetryPoint.model_validate(row[0]) for row in rows):
        point_utc = _utc(point.timestamp)
        point_source = str(getattr(point, "source_id", ""))
        merge_index = None
        merge_sources: set[str] = set()
        for index in range(len(combined) - 1, -1, -1):
            prior = combined[index]
            prior_utc = _utc(prior.timestamp)
            if prior_utc < point_utc:
                break
            prior_sources = set(str(getattr(prior, "source_id", "")).split("+")) - {""}
            if prior_utc == point_utc and prior_sources and point_source and point_source not in prior_sources:
                merge_index, merge_sources = index, prior_sources
                break
        if merge_index is None:
            combined.append(point)
            continue
        merged = combined[merge_index].model_dump()
        for field in TelemetryPoint.model_fields:
            value = getattr(point, field)
            if field != "timestamp" and value is not None and merged.get(field) is None:
                merged[field] = value
        merge_sources.add(point_source)
        merged["source_id"] = "+".join(sorted(merge_sources))
        combined[merge_index] = TelemetryPoint.model_validate(merged)
    return combined


def list_wells() -> list[dict]:
    """Wells with telemetry, history or a source status: telemetry count and time span, history count."""
    ids = union(select(telemetry.c.well_id), select(history_events.c.well_id),
                select(source_health.c.well_id)).subquery("w")
    history_count = (select(func.count()).select_from(history_events)
                     .where(history_events.c.well_id == ids.c.well_id).scalar_subquery())
    query = (select(ids.c.well_id, func.count(telemetry.c.id).label("record_count"),
                    func.min(telemetry.c.timestamp).label("first_timestamp"),
                    func.max(telemetry.c.timestamp).label("last_timestamp"),
                    history_count.label("history_count"))
             .select_from(ids.outerjoin(telemetry, telemetry.c.well_id == ids.c.well_id))
             .group_by(ids.c.well_id).order_by(ids.c.well_id))
    with engine().connect() as conn:
        return [dict(row) for row in conn.execute(query).mappings().all()]


def known_well_ids() -> list[str]:
    """Wells the system knows from any source, the rating or MAI_WELL_IDS."""
    query = union(select(telemetry.c.well_id), select(history_events.c.well_id),
                  select(well_rating.c.well_id), select(source_health.c.well_id))
    with engine().connect() as conn:
        wells = {row[0] for row in conn.execute(query).all()}
    return sorted(wells | set(settings.configured_well_ids))


@_retry_deadlocks
def save_history(well_id: str, requested_batch_id: str | None,
                 work_history: list[WorkHistoryItem], failure_history: list[FailureHistoryItem],
                 source_id: str | None = None, replace: bool = False) -> dict:
    """Store work and failure events; an event already stored for the source is skipped. A batch id
    used again: as in save_batch."""
    batch_id = requested_batch_id or str(uuid.uuid4())
    if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 128:
        source_id = _UNSPECIFIED_SOURCE
    encoded: list[dict] = []
    for event_type, records, date_field in (("work", work_history, "date"),
                                             ("failure", failure_history, "failure_date")):
        for index, record in enumerate(records):
            event_date = getattr(record, date_field)
            payload = record.model_dump(mode="json")
            encoded.append({"event_type": event_type, "record_index": index, "source_id": source_id,
                            "event_date": event_date, "payload_json": payload,
                            "event_fingerprint": _json_hash({"event_type": event_type,
                                                             "event_date": event_date.isoformat(),
                                                             "payload": payload})})
    digest = _json_hash([{**item, "event_date": item["event_date"].isoformat()} for item in encoded])
    stored = {"work": 0, "failure": 0}
    batch = {"batch_id": batch_id, "well_id": well_id, "work_count": len(work_history),
             "failure_count": len(failure_history), "payload_sha256": digest, "created_at": datetime.now(timezone.utc)}
    with engine().begin() as conn:
        _queue_writes(conn, "history")
        if not _merge(conn, history_batches, [batch], ("batch_id",)):
            existing = conn.execute(select(history_batches).where(
                history_batches.c.batch_id == batch_id)).mappings().one()
            if existing["well_id"] == well_id and existing["payload_sha256"] == digest:
                return {"batch_id": batch_id, "well_id": well_id,
                        "work_records_received": len(work_history), "failure_records_received": len(failure_history),
                        "work_records_stored": existing["work_count"],
                        "failure_records_stored": existing["failure_count"], "idempotent_replay": True}
            if not replace:
                raise ValueError("history batch_id has already been used with a different well or payload")
            conn.execute(delete(history_batches).where(history_batches.c.batch_id == batch_id))  # its events too
            _merge(conn, history_batches, [batch], ("batch_id",))
        for event_type in ("work", "failure"):
            event_rows = [{"batch_id": batch_id, "well_id": well_id, **item}
                          for item in encoded if item["event_type"] == event_type]
            if event_rows:
                stored[event_type] = len(_merge(conn, history_events, event_rows,
                                                ("well_id", "source_id", "event_type", "event_fingerprint"),
                                                output="id"))
    return {"batch_id": batch_id, "well_id": well_id,
            "work_records_received": len(work_history), "failure_records_received": len(failure_history),
            "work_records_stored": stored["work"], "failure_records_stored": stored["failure"],
            "idempotent_replay": False}


def load_history_events(well_id: str) -> list[dict]:
    """History events with their source and date, for the rating evidence and provenance."""
    with engine().connect() as conn:
        rows = conn.execute(select(history_events.c.event_type, history_events.c.source_id,
                                   history_events.c.event_date, history_events.c.payload_json)
                            .where(history_events.c.well_id == well_id)
                            .order_by(history_events.c.event_date, history_events.c.id)).mappings().all()
    return [{"event_type": row["event_type"],
             "source_id": None if row["source_id"] == _UNSPECIFIED_SOURCE else row["source_id"],
             "event_date": row["event_date"], "payload": row["payload_json"]} for row in rows]


def load_history(well_id: str) -> dict[str, list]:
    work: list[WorkHistoryItem] = []
    failures: list[FailureHistoryItem] = []
    for event in load_history_events(well_id):
        if event["event_type"] == "work":
            work.append(WorkHistoryItem.model_validate(event["payload"]))
        else:
            failures.append(FailureHistoryItem.model_validate(event["payload"]))
    return {"work_history": work, "failure_history": failures}


@_retry_deadlocks
def record_source_status(source_id: str, well_id: str, status: str, message: str | None = None,
                         record_count: int | None = None, origin: str = "api_refresh") -> dict:
    row = {"source_id": source_id, "well_id": well_id,
           "origin": origin if origin in _SOURCE_ORIGINS else "api_refresh", "status": status,
           "message": message, "record_count": record_count, "retrieved_at": datetime.now(timezone.utc)}
    with engine().begin() as conn:
        _merge(conn, source_health, [row], ("source_id", "well_id", "origin"),
               update=("status", "message", "record_count", "retrieved_at"))
    return row


def list_source_statuses(well_id: str | None = None) -> list[dict]:
    query = select(source_health).order_by(source_health.c.source_id, source_health.c.well_id)
    if well_id is not None:
        query = query.where(source_health.c.well_id == well_id)
    with engine().connect() as conn:
        return [dict(row) for row in conn.execute(query).mappings().all()]


def effective_source_statuses(well_id: str | None = None) -> list[dict]:
    """One status per (source, well): the latest from an API (push, pull, adapter, system) other than
    not_configured, else the latest manual upload, else the latest row."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in list_source_statuses(well_id):
        grouped.setdefault((str(row["source_id"]), str(row["well_id"])), []).append(row)
    effective = []
    for key in sorted(grouped):
        items = sorted(grouped[key], key=lambda row: row["retrieved_at"], reverse=True)
        live = next((row for row in items if row["origin"] in {"api_push", "api_refresh", "adapter", "system"}
                     and row["status"] != "not_configured"), None)
        manual = next((row for row in items if row["origin"] == "manual_upload"
                       and row["status"] != "not_configured"), None)
        if live is not None:
            chosen = dict(live)
        elif manual is not None:
            chosen = dict(manual)
            note = "Ручная загрузка активна; внешний источник не настроен."
            chosen["message"] = f"{str(chosen.get('message') or '').strip()} {note}".strip()
        else:
            chosen = dict(items[0])
        effective.append(chosen)
    return effective


@_retry_deadlocks
def save_source_payload(source_id: str, well_id: str, payload: dict) -> None:
    with engine().begin() as conn:
        _merge(conn, source_documents, [{"source_id": source_id, "well_id": well_id, "payload_json": payload,
                                         "updated_at": datetime.now(timezone.utc)}],
               ("source_id", "well_id"), update=("payload_json", "updated_at"))


def load_source_payloads(well_id: str) -> dict[str, dict]:
    with engine().connect() as conn:
        rows = conn.execute(select(source_documents.c.source_id, source_documents.c.payload_json)
                            .where(source_documents.c.well_id == well_id)).all()
    return {source_id: payload for source_id, payload in rows}


def load_extra_measurements(well_id: str) -> list[dict]:
    """Rows with additional indicators (pH, hardness…) of a well, oldest first."""
    extras = func.json_query(telemetry.c.payload_json, "$.extra_measurements")
    query = (select(telemetry.c.source_id, telemetry.c.timestamp, extras.label("extras"))
             .where(telemetry.c.well_id == well_id, func.left(extras, 1) == literal_column("'{'"))
             .order_by(telemetry.c.timestamp, telemetry.c.id))
    with engine().connect() as conn:
        rows = conn.execute(query).mappings().all()
    return [{"source_id": None if row["source_id"] == _UNSPECIFIED_SOURCE else row["source_id"],
             "timestamp": row["timestamp"],
             "extras": json.loads(row["extras"]) if isinstance(row["extras"], str) else row["extras"]}
            for row in rows]


def well_has_data(well_id: str) -> bool:
    """Whether any source data of the well remain: telemetry, history events or source documents."""
    with engine().connect() as conn:
        for table in (telemetry, history_events, source_documents):
            if conn.execute(select(table.c.well_id).where(table.c.well_id == well_id).limit(1)).first():
                return True
    return False


@_retry_deadlocks
def forget_well(well_id: str) -> None:
    """Remove what was derived for a well that has no data left (its rating row and source
    statuses), e.g. a well a deleted document had created."""
    with engine().begin() as conn:
        for table in (well_rating, source_health):
            conn.execute(delete(table).where(table.c.well_id == well_id))
