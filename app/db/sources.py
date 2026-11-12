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
                           source_documents, source_health, telemetry, well_rating, co2_assessments, document_contributions)
from app.db.documents import contribution_rows
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
    """Merge a display slice without losing the source of each field or document ownership."""
    with engine().connect() as conn:
        rows = conn.execute(select(telemetry.c.payload_json, ingestion_batches.c.created_at)
            .join(ingestion_batches, telemetry.c.batch_id == ingestion_batches.c.batch_id)
            .where(telemetry.c.well_id == well_id).order_by(telemetry.c.id)).all()
    versions = [(payload, when) for payload, when in rows]
    for row in contribution_rows(well_id):
        versions.extend((payload, row["updated_at"]) for payload in row["data"].get("records", []))
    # Identical observations from independent owners remain stored separately, exposed once.
    selected = {}
    occurrences = {}
    for payload, updated in sorted(versions, key=lambda item:item[1]):
        point = TelemetryPoint.model_validate(payload)
        source = str(getattr(point, "source_id", "") or "")
        key = (source, _utc(point.timestamp), updated)
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        selected[(source, _utc(point.timestamp), occurrence)] = point
    combined, group = [], []
    for point in sorted(selected.values(), key=lambda p:_utc(p.timestamp)):
        source = str(getattr(point, "source_id", "") or "")
        fields = {key: source for key in TelemetryPoint.model_fields if key != "timestamp" and getattr(point, key) is not None}
        if group and _utc(group[0].timestamp) != _utc(point.timestamp):
            group = []
        previous = next((p for p in reversed(group) if source not in str(getattr(p, "source_id", "")).split("+")), None)
        if previous is None:
            merged = point.model_copy(update={"field_sources": fields})
            combined.append(merged)
            group.append(merged)
            continue
        for key in fields:
            if getattr(previous, key) is None:
                setattr(previous, key, getattr(point, key))
                previous.field_sources[key] = source
        previous.source_id = "+".join(sorted(set(previous.source_id.split("+")) | {source}))
    return combined


def list_wells() -> list[dict]:
    """Stored observation/event counts and time span, including document-owned data."""
    ids = union(select(telemetry.c.well_id), select(history_events.c.well_id),
                select(source_health.c.well_id), select(co2_assessments.c.well_id), select(document_contributions.c.well_id)).subquery("w")
    history_count = (select(func.count()).select_from(history_events)
                     .where(history_events.c.well_id == ids.c.well_id).scalar_subquery())
    query = (select(ids.c.well_id, func.count(telemetry.c.id).label("record_count"),
                    func.min(telemetry.c.timestamp).label("first_timestamp"),
                    func.max(telemetry.c.timestamp).label("last_timestamp"),
                    history_count.label("history_count"))
             .select_from(ids.outerjoin(telemetry, telemetry.c.well_id == ids.c.well_id))
             .group_by(ids.c.well_id).order_by(ids.c.well_id))
    with engine().connect() as conn:
        result = {row['well_id']: dict(row) for row in conn.execute(query).mappings()}
        for well_id, data in conn.execute(select(document_contributions.c.well_id, document_contributions.c.data)):
            row = result[well_id]
            records = data.get('records', [])
            row['record_count'] += len(records)
            row['history_count'] += len(data.get('work_history', [])) + len(data.get('failure_history', []))
            moments = [_utc(datetime.fromisoformat(item['timestamp'])) for item in records]
            if moments:
                row['first_timestamp'] = min(moments + ([row['first_timestamp']] if row['first_timestamp'] else []))
                row['last_timestamp'] = max(moments + ([row['last_timestamp']] if row['last_timestamp'] else []))
        return list(result.values())


def known_well_ids() -> list[str]:
    """Wells the system knows from any source, the rating or MAI_WELL_IDS."""
    query = union(select(telemetry.c.well_id), select(history_events.c.well_id),
                  select(well_rating.c.well_id), select(source_health.c.well_id), select(co2_assessments.c.well_id), select(document_contributions.c.well_id))
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
    result = [{"event_type": row["event_type"],
               "source_id": None if row["source_id"] == _UNSPECIFIED_SOURCE else row["source_id"],
               "event_date": row["event_date"], "payload": row["payload_json"]} for row in rows]
    for row in contribution_rows(well_id):
        for key, kind, model, date_key in (("work_history", "work", WorkHistoryItem, "date"),
                                           ("failure_history", "failure", FailureHistoryItem, "failure_date")):
            for raw in row["data"].get(key, []):
                item = model.model_validate(raw)
                result.append({"event_type": kind, "source_id": row["source_id"],
                    "event_date": _utc(getattr(item, date_key)), "payload": item.model_dump(mode="json")})
    unique = {(r["source_id"], r["event_type"], _json_hash(r["payload"])): r for r in result}
    return sorted(unique.values(), key=lambda r:r["event_date"])


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


def latest_source_statuses() -> dict[str, dict]:
    """Last attempt per source, including its origin; bounded by the number of sources."""
    ranked = select(*source_health.c, func.row_number().over(
        partition_by=source_health.c.source_id,
        order_by=(source_health.c.retrieved_at.desc(), source_health.c.well_id)).label("position")).subquery()
    with engine().connect() as conn:
        return {r["source_id"]: dict(r) for r in conn.execute(select(ranked).where(ranked.c.position == 1)).mappings()}


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
    result = {source_id: payload for source_id, payload in rows}
    for row in contribution_rows(well_id):
        if row["data"].get("payload"):
            result[row["source_id"]] = row["data"]["payload"]
    return result


def load_extra_measurements(well_id: str) -> list[dict]:
    return [{"source_id": getattr(point, "source_id", None), "timestamp": point.timestamp,
             "extras": point.extra_measurements} for point in load_telemetry(well_id) if point.extra_measurements]


def well_has_data(well_id: str) -> bool:
    """Whether any source data of the well remain: telemetry, history events or source documents."""
    with engine().connect() as conn:
        for table in (telemetry, history_events, source_documents, co2_assessments, document_contributions):
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
