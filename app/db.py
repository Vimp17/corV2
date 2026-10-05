"""PostgreSQL repository boundary for ingestion, analysis history and source health."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, JSON, MetaData, String, Table, Column, UniqueConstraint, create_engine, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app.config import settings
from app.schemas import FailureHistoryItem, PipelineResult, TelemetryPoint, WorkHistoryItem

metadata = MetaData()
ingestion_batches = Table(
    "ingestion_batches", metadata,
    Column("batch_id", String(128), primary_key=True), Column("well_id", String(128), nullable=False),
    Column("record_count", Integer, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False),
    Column("payload_sha256", String(64), nullable=False),
)
telemetry = Table(
    "telemetry", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("batch_id", String(128), ForeignKey("ingestion_batches.batch_id", ondelete="CASCADE"), nullable=False),
    Column("well_id", String(128), nullable=False), Column("source_id", String(128)),
    Column("source_occurrence", Integer), Column("record_index", Integer, nullable=False),
    Column("timestamp", DateTime(timezone=True), nullable=False), Column("payload_json", JSON, nullable=False),
    UniqueConstraint("batch_id", "record_index", name="uq_telemetry_batch_record"),
    UniqueConstraint("well_id", "source_id", "timestamp", "source_occurrence",
                     name="uq_telemetry_source_observation"),
)
Index("idx_telemetry_well_time", telemetry.c.well_id, telemetry.c.timestamp)
history_batches = Table(
    "history_batches", metadata,
    Column("batch_id", String(128), primary_key=True), Column("well_id", String(128), nullable=False),
    Column("work_count", Integer, nullable=False), Column("failure_count", Integer, nullable=False),
    Column("payload_sha256", String(64), nullable=False), Column("created_at", DateTime(timezone=True), nullable=False),
)
history_events = Table(
    "history_events", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("batch_id", String(128), ForeignKey("history_batches.batch_id", ondelete="CASCADE"), nullable=False),
    Column("well_id", String(128), nullable=False), Column("source_id", String(128)),
    Column("event_type", String(16), nullable=False), Column("event_fingerprint", String(64), nullable=False),
    Column("record_index", Integer, nullable=False), Column("event_date", DateTime(timezone=True), nullable=False),
    Column("payload_json", JSON, nullable=False),
    UniqueConstraint("batch_id", "event_type", "record_index", name="uq_history_batch_event"),
    UniqueConstraint("well_id", "source_id", "event_type", "event_fingerprint",
                     name="uq_history_source_event"),
)
Index("idx_history_well_time", history_events.c.well_id, history_events.c.event_date)
analysis_runs = Table(
    "analysis_runs", metadata,
    Column("analysis_id", String(64), primary_key=True), Column("well_id", String(128), nullable=False),
    Column("request_id", String(128)), Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status", String(64), nullable=False), Column("model_versions_json", JSON, nullable=False),
    Column("result_json", JSON, nullable=False), Column("agent_status", String(64)),
    Column("agent_updated_at", DateTime(timezone=True)), Column("agent_response_json", JSON),
    Column("external_data_json", JSON),
)
Index("idx_analysis_runs_well_time", analysis_runs.c.well_id, analysis_runs.c.created_at)
source_health = Table(
    "source_health",
    metadata,
    Column("source_id", String(128), primary_key=True),
    Column("well_id", String(128), primary_key=True),
    Column(
        "origin",
        String(32),
        primary_key=True,
        nullable=False,
        server_default="api_refresh",
    ),
    Column("status", String(32), nullable=False),
    Column("message", String(2000)),
    Column("record_count", Integer),
    Column("retrieved_at", DateTime(timezone=True), nullable=False),
)

source_documents = Table(
    "source_documents", metadata,
    Column("source_id", String(128), primary_key=True), Column("well_id", String(128), primary_key=True),
    Column("payload_json", JSON, nullable=False), Column("updated_at", DateTime(timezone=True), nullable=False),
)

_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    return _engine


def init_db() -> None:
    metadata.create_all(engine())
    _migrate_source_health_origin()


def check_database() -> bool:
    with engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return True

_SOURCE_ORIGINS = {
    "api_push",
    "manual_upload",
    "api_refresh",
    "adapter",
    "system",
}


def _normalize_origin(origin: str | None) -> str:
    origin = str(origin or "").strip()
    return origin if origin in _SOURCE_ORIGINS else "api_refresh"


def _json_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def save_batch(well_id: str, requested_batch_id: str | None, records: Iterable[TelemetryPoint]) -> dict:
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
            source_id = None
            payload.pop("source_id", None)
        source_occurrence = None
        if source_id:
            timestamp = point.timestamp.replace(tzinfo=timezone.utc) if point.timestamp.tzinfo is None else point.timestamp.astimezone(timezone.utc)
            occurrence_key = (source_id, timestamp.isoformat())
            source_occurrence = occurrences.get(occurrence_key, 0)
            occurrences[occurrence_key] = source_occurrence + 1
        telemetry_rows.append({"batch_id": batch_id, "well_id": well_id, "source_id": source_id,
                               "source_occurrence": source_occurrence, "record_index": index,
                               "timestamp": point.timestamp, "payload_json": payload})
    digest = _json_hash(payloads)
    created_at = datetime.now(timezone.utc)
    with engine().begin() as conn:
        inserted = conn.execute(pg_insert(ingestion_batches).values(
            batch_id=batch_id, well_id=well_id, record_count=len(rows), created_at=created_at,
            payload_sha256=digest,
        ).on_conflict_do_nothing(index_elements=[ingestion_batches.c.batch_id]))
        if inserted.rowcount == 0:
            existing = conn.execute(select(ingestion_batches).where(
                ingestion_batches.c.batch_id == batch_id)).mappings().one()
            if existing["well_id"] != well_id or existing["payload_sha256"] != digest:
                raise ValueError("batch_id has already been used with a different well or payload")
            return {"batch_id": batch_id, "well_id": well_id, "records_received": len(rows),
                    "records_stored": existing["record_count"], "idempotent_replay": True}
        if telemetry_rows:
            statement = pg_insert(telemetry).values(telemetry_rows)
            statement = statement.on_conflict_do_update(
                index_elements=[telemetry.c.well_id, telemetry.c.source_id,
                                telemetry.c.timestamp, telemetry.c.source_occurrence],
                set_={"batch_id": statement.excluded.batch_id,
                      "record_index": statement.excluded.record_index,
                      "payload_json": statement.excluded.payload_json},
            )
            conn.execute(statement)
    return {"batch_id": batch_id, "well_id": well_id, "records_received": len(rows),
            "records_stored": len(rows), "idempotent_replay": False}


def load_telemetry(well_id: str) -> list[TelemetryPoint]:
    with engine().connect() as conn:
        rows = conn.execute(select(telemetry.c.payload_json).where(
            telemetry.c.well_id == well_id).order_by(telemetry.c.timestamp, telemetry.c.id)).all()
    points = [TelemetryPoint.model_validate(row[0]) for row in rows]
    combined: list[TelemetryPoint] = []
    for point in points:
        point_utc = point.timestamp.replace(tzinfo=timezone.utc) if point.timestamp.tzinfo is None else point.timestamp.astimezone(timezone.utc)
        point_source = str(getattr(point, "source_id", ""))
        merge_index = None
        merge_sources: set[str] = set()
        for index in range(len(combined) - 1, -1, -1):
            prior = combined[index]
            prior_utc = prior.timestamp.replace(tzinfo=timezone.utc) if prior.timestamp.tzinfo is None else prior.timestamp.astimezone(timezone.utc)
            if prior_utc < point_utc:
                break
            prior_sources = set(str(getattr(prior, "source_id", "")).split("+")) - {""}
            if prior_utc == point_utc and prior_sources and point_source and point_source not in prior_sources:
                merge_index, merge_sources = index, prior_sources
                break
        if merge_index is not None:
            prior = combined[merge_index]
            merged = prior.model_dump()
            for field in TelemetryPoint.model_fields:
                value = getattr(point, field)
                if field == "timestamp" or value is None:
                    continue
                if merged.get(field) is None:
                    merged[field] = value
            merge_sources.add(point_source)
            merged["source_id"] = "+".join(sorted(merge_sources))
            combined[merge_index] = TelemetryPoint.model_validate(merged)
        else:
            combined.append(point)
    return combined


def list_wells() -> list[dict]:
    query = text("""
        WITH well_ids AS (
            SELECT well_id FROM telemetry UNION SELECT well_id FROM history_events
            UNION SELECT well_id FROM analysis_runs UNION SELECT well_id FROM source_health
        )
        SELECT w.well_id, COUNT(t.id) AS record_count, MIN(t.timestamp) AS first_timestamp,
               MAX(t.timestamp) AS last_timestamp,
               (SELECT COUNT(*) FROM history_events h WHERE h.well_id=w.well_id) AS history_count
        FROM well_ids w LEFT JOIN telemetry t ON t.well_id=w.well_id
        GROUP BY w.well_id ORDER BY w.well_id
    """)
    with engine().connect() as conn:
        return [dict(row) for row in conn.execute(query).mappings().all()]


def save_history(well_id: str, requested_batch_id: str | None,
                 work_history: list[WorkHistoryItem], failure_history: list[FailureHistoryItem],
                 source_id: str | None = None) -> dict:
    batch_id = requested_batch_id or str(uuid.uuid4())
    if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 128:
        source_id = None
    encoded: list[dict] = []
    for event_type, records, date_field in (("work", work_history, "date"),
                                             ("failure", failure_history, "failure_date")):
        for index, record in enumerate(records):
            event_date = getattr(record, date_field)
            payload = record.model_dump(mode="json")
            event = {"event_type": event_type, "record_index": index,
                     "source_id": source_id, "event_date": event_date,
                     "payload_json": payload}
            event["event_fingerprint"] = _json_hash({
                "event_type": event_type, "event_date": event_date.isoformat(), "payload": payload,
            })
            encoded.append(event)
    digest = _json_hash([{**item, "event_date": item["event_date"].isoformat()} for item in encoded])
    inserted_counts = {"work": 0, "failure": 0}
    with engine().begin() as conn:
        inserted = conn.execute(pg_insert(history_batches).values(
            batch_id=batch_id, well_id=well_id, work_count=len(work_history),
            failure_count=len(failure_history), payload_sha256=digest,
            created_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing(index_elements=[history_batches.c.batch_id]))
        if inserted.rowcount == 0:
            existing = conn.execute(select(history_batches).where(
                history_batches.c.batch_id == batch_id)).mappings().one()
            if existing["well_id"] != well_id or existing["payload_sha256"] != digest:
                raise ValueError("history batch_id has already been used with a different well or payload")
            return {"batch_id": batch_id, "well_id": well_id,
                    "work_records_received": len(work_history), "failure_records_received": len(failure_history),
                    "work_records_stored": existing["work_count"],
                    "failure_records_stored": existing["failure_count"], "idempotent_replay": True}
        if encoded:
            for event_type in ("work", "failure"):
                event_rows = [{"batch_id": batch_id, "well_id": well_id, **item}
                              for item in encoded if item["event_type"] == event_type]
                if not event_rows:
                    continue
                statement = pg_insert(history_events).values(event_rows).on_conflict_do_nothing(
                    index_elements=[history_events.c.well_id, history_events.c.source_id,
                                    history_events.c.event_type, history_events.c.event_fingerprint]
                )
                inserted_counts[event_type] = max(0, conn.execute(statement).rowcount or 0)
    return {"batch_id": batch_id, "well_id": well_id,
            "work_records_received": len(work_history), "failure_records_received": len(failure_history),
            "work_records_stored": inserted_counts["work"],
            "failure_records_stored": inserted_counts["failure"], "idempotent_replay": False}


def load_history(well_id: str) -> dict[str, list]:
    with engine().connect() as conn:
        rows = conn.execute(select(history_events.c.event_type, history_events.c.payload_json)
                            .where(history_events.c.well_id == well_id)
                            .order_by(history_events.c.event_date, history_events.c.id)).all()
    work: list[WorkHistoryItem] = []
    failures: list[FailureHistoryItem] = []
    for event_type, payload in rows:
        (work if event_type == "work" else failures).append(
            WorkHistoryItem.model_validate(payload) if event_type == "work"
            else FailureHistoryItem.model_validate(payload))
    return {"work_history": work, "failure_history": failures}


def save_analysis_run(result: PipelineResult | dict, request_id: str | None = None) -> dict:
    payload = result.model_dump(mode="json") if isinstance(result, PipelineResult) else result
    analysis_id = payload["analysis_id"]
    model_versions = {item["model_id"]: item["model_version"]
                      for item in payload.get("model_predictions", [])}
    created_at = datetime.now(timezone.utc)
    with engine().begin() as conn:
        conn.execute(analysis_runs.insert().values(
            analysis_id=analysis_id, well_id=payload["well_id"], request_id=request_id,
            created_at=created_at, status=payload["status"], model_versions_json=model_versions,
            result_json=payload,
        ))
    return {"analysis_id": analysis_id, "well_id": payload["well_id"],
            "created_at": created_at.isoformat(), "status": payload["status"],
            "model_versions": model_versions, "request_id": request_id}


def save_agent_result(analysis_id: str, well_id: str, agent_status: str,
                      agent_response: dict | None, external_data: dict | None) -> bool:
    with engine().begin() as conn:
        result = conn.execute(analysis_runs.update().where(
            analysis_runs.c.analysis_id == analysis_id,
            analysis_runs.c.well_id == well_id,
        ).values(agent_status=agent_status, agent_updated_at=datetime.now(timezone.utc),
                 agent_response_json=agent_response, external_data_json=external_data))
        return result.rowcount == 1


def get_analysis_run(analysis_id: str) -> dict | None:
    with engine().connect() as conn:
        row = conn.execute(select(analysis_runs).where(
            analysis_runs.c.analysis_id == analysis_id)).mappings().first()
    if row is None:
        return None
    return {"analysis_id": row["analysis_id"], "well_id": row["well_id"],
            "request_id": row["request_id"], "created_at": row["created_at"].isoformat(),
            "status": row["status"], "model_versions": row["model_versions_json"],
            "analysis": row["result_json"], "agent_status": row["agent_status"],
            "agent_updated_at": row["agent_updated_at"].isoformat() if row["agent_updated_at"] else None,
            "agent_response": row["agent_response_json"], "external_data": row["external_data_json"]}


def record_source_status(
    source_id: str,
    well_id: str,
    status: str,
    message: str | None = None,
    record_count: int | None = None,
    origin: str = "api_refresh",
) -> dict:
    origin = _normalize_origin(origin)
    retrieved_at = datetime.now(timezone.utc)

    statement = pg_insert(source_health).values(
        source_id=source_id,
        well_id=well_id,
        origin=origin,
        status=status,
        message=message,
        record_count=record_count,
        retrieved_at=retrieved_at,
    ).on_conflict_do_update(
        index_elements=[
            source_health.c.source_id,
            source_health.c.well_id,
            source_health.c.origin,
        ],
        set_={
            "status": status,
            "message": message,
            "record_count": record_count,
            "retrieved_at": retrieved_at,
        },
    )

    with engine().begin() as conn:
        conn.execute(statement)

    return {
        "source_id": source_id,
        "well_id": well_id,
        "origin": origin,
        "status": status,
        "message": message,
        "record_count": record_count,
        "retrieved_at": retrieved_at,
    }


def list_source_statuses(well_id: str | None = None) -> list[dict]:
    query = select(source_health)
    if well_id is not None:
        query = query.where(source_health.c.well_id == well_id)
    with engine().connect() as conn:
        return [dict(row) for row in conn.execute(query.order_by(source_health.c.source_id,
                                                                 source_health.c.well_id)).mappings().all()]


def save_source_payload(source_id: str, well_id: str, payload: dict) -> None:
    statement = pg_insert(source_documents).values(
        source_id=source_id, well_id=well_id, payload_json=payload,
        updated_at=datetime.now(timezone.utc),
    ).on_conflict_do_update(
        index_elements=[source_documents.c.source_id, source_documents.c.well_id],
        set_={"payload_json": payload, "updated_at": datetime.now(timezone.utc)},
    )
    with engine().begin() as conn:
        conn.execute(statement)


def load_source_payloads(well_id: str) -> dict[str, dict]:
    with engine().connect() as conn:
        rows = conn.execute(select(source_documents.c.source_id, source_documents.c.payload_json)
                            .where(source_documents.c.well_id == well_id)).all()
    return {source_id: payload for source_id, payload in rows}


def latest_analysis_runs() -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(select(analysis_runs).order_by(analysis_runs.c.created_at.desc())).mappings().all()
    latest: dict[str, dict] = {}
    for row in rows:
        if row["well_id"] in latest:
            continue
        latest[row["well_id"]] = {
            "analysis_id": row["analysis_id"], "well_id": row["well_id"],
            "created_at": row["created_at"].isoformat(), "status": row["status"],
            "analysis": row["result_json"], "agent_response": row["agent_response_json"],
            "agent_status": row["agent_status"],
            "agent_updated_at": row["agent_updated_at"].isoformat() if row["agent_updated_at"] else None,
        }
    return list(latest.values())

def _migrate_source_health_origin() -> None:
    statements = [
        """
        ALTER TABLE source_health
        ADD COLUMN IF NOT EXISTS origin VARCHAR(32) NOT NULL DEFAULT 'api_refresh'
        """,
        """
        UPDATE source_health
        SET origin = 'api_refresh'
        WHERE origin IS NULL
        """,
        """
        DO $$
        DECLARE
            rec RECORD;
        BEGIN
            FOR rec IN
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'source_health'::regclass
                  AND contype = 'p'
            LOOP
                EXECUTE format('ALTER TABLE source_health DROP CONSTRAINT %I', rec.conname);
            END LOOP;
        END $$;
        """,
        """
        ALTER TABLE source_health
        ADD PRIMARY KEY (source_id, well_id, origin)
        """,
    ]

    with engine().begin() as conn:
        for statement in statements:
            conn.execute(text(statement))

def effective_source_statuses(well_id: str | None = None) -> list[dict]:
    rows = list_source_statuses(well_id)

    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (
            str(row.get("source_id") or ""),
            str(row.get("well_id") or ""),
        )
        grouped.setdefault(key, []).append(row)

    effective: list[dict] = []
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def sort_key(row: dict):
        value = row.get("retrieved_at")
        if value is None:
            return epoch
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    for _, items in grouped.items():
        items.sort(key=sort_key, reverse=True)

        live = next(
            (
                row
                for row in items
                  if row.get("origin") in {"api_push", "api_refresh", "adapter", "system"}
                and row.get("status") != "not_configured"
            ),
            None,
        )

        manual = next(
            (
                row
                for row in items
                if row.get("origin") == "manual_upload"
                and row.get("status") != "not_configured"
            ),
            None,
        )

        if live is not None:
            chosen = dict(live)
        elif manual is not None:
            chosen = dict(manual)
            message = str(chosen.get("message") or "").strip()
            note = "Ручная загрузка активна; внешний источник не настроен."
            chosen["message"] = f"{message} {note}".strip() if message else note
        else:
            chosen = dict(items[0])

        effective.append(chosen)

    effective.sort(
        key=lambda row: (
            str(row.get("source_id") or ""),
            str(row.get("well_id") or ""),
        )
    )

    return effective
