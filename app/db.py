"""PostgreSQL repository boundary for ingestion, analysis history and source health."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Iterable, Iterator

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, MetaData, String, Table, Text, Column, UniqueConstraint, create_engine, delete, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
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
    Column("external_data_json", JSON), Column("horizon_days", Integer),
)
Index("idx_analysis_runs_well_time", analysis_runs.c.well_id, analysis_runs.c.created_at)
Index("idx_analysis_runs_well_horizon_time", analysis_runs.c.well_id,
      analysis_runs.c.horizon_days, analysis_runs.c.created_at)
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

monitor_jobs = Table(
    "monitor_jobs", metadata,
    Column("job_id", String(64), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("horizon_days", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("result_json", JSON),
    Column("error", String(2000)),
)

# Current rating per well. Evidence and model scores are computed from source data by
# app.core.rating_builder (provenance records the source and date of each value); plan_date,
# promine_status and comment are operator fields edited on the dashboard and never overwritten
# by a recomputation. The index, measure and deadline are derived in app.core.rating.
well_rating = Table(
    "well_rating", metadata,
    Column("well_id", String(128), primary_key=True),
    Column("field", String(256)), Column("equipment", String(256)),
    Column("in_operation", Boolean), Column("sampling", Boolean),
    Column("corrosive_environment", Boolean),
    Column("tonnage_t", Float), Column("noo_days", Float),
    Column("demount_act", Boolean), Column("demount_corrosion", Boolean),
    Column("base_act", Boolean), Column("base_corrosion", Boolean),
    Column("category", String(32)),
    Column("model_ntc", Float), Column("model_math", Float),
    Column("plan_date", Date), Column("promine_status", String(32)),
    Column("comment", String(2000)),
    Column("assessed_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("source", String(32), nullable=False),
    Column("measure", String(32)), Column("measure_since", DateTime(timezone=True)),
    Column("provenance", JSON), Column("computed_at", DateTime(timezone=True)),
    Column("skip_reason", String(2000)), Column("analysis_id", String(64)),
    Column("protection", String(32)), Column("model_ntc_raw", Float),
    Column("installed_at", DateTime(timezone=True)), Column("liquid_rate_m3_day", Float),
    Column("oil_pct", Float), Column("data_gaps", JSON),
    # Latest proposal drafted by the AI agent from all well data (app.core.proposal_agent).
    Column("ai_proposal", JSON), Column("ai_proposal_at", DateTime(timezone=True)),
)

# Proposals to change the ERA repair plan, sent through Промайн.
era_proposals = Table(
    "era_proposals", metadata,
    Column("proposal_id", String(64), primary_key=True),
    Column("well_id", String(128), nullable=False),
    Column("change_type", String(32), nullable=False),
    Column("reason", String(4000), nullable=False),
    Column("proposed_date", Date),
    Column("author", String(200)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("transport", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("sent_at", DateTime(timezone=True)),
    Column("external_id", String(256)),
    Column("error", String(2000)),
    Column("subject", String(500), nullable=False),
    Column("body", String(20000), nullable=False),
    Column("rating_json", JSON),
)
Index("idx_era_proposals_well_time", era_proposals.c.well_id, era_proposals.c.created_at)

source_documents = Table(
    "source_documents", metadata,
    Column("source_id", String(128), primary_key=True), Column("well_id", String(128), primary_key=True),
    Column("payload_json", JSON, nullable=False), Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Documents received as files (manual upload or the inbox folder): the original is stored on
# disk, the extracted text and fields here. Data taken from a document is written to the usual
# telemetry / history tables under batch ids "doc-<document_id>-<well_id>".
documents = Table(
    "documents", metadata,
    Column("document_id", String(36), primary_key=True),
    Column("sha256", String(64), nullable=False, unique=True),
    Column("filename", String(512), nullable=False),
    Column("channel", String(16), nullable=False),
    Column("file_kind", String(16), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("storage_path", String(1024), nullable=False),
    Column("status", String(32), nullable=False),
    Column("status_message", String(2000)),
    Column("pages", Integer),
    Column("doc_type", String(64)),
    Column("well_ids", JSON),
    Column("doc_date", Date),
    Column("text_method", String(32)),
    Column("text_chars", Integer),
    Column("text", Text),
    Column("extraction", JSON),
    Column("loaded", JSON),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("processed_at", DateTime(timezone=True)),
)
Index("idx_documents_status_created", documents.c.status, documents.c.created_at)
Index("idx_documents_created", documents.c.created_at)

_engine: Engine | None = None

# PostgreSQL allows at most 65,535 bind parameters per statement; multi-row inserts are
# split so that the documented 10,000-row batches stay well below that limit.
_INSERT_CHUNK_ROWS = 2000
# Unique constraints treat NULLs as distinct, so a NULL source_id would let identical rows
# be stored twice. Rows without a valid source are keyed under this sentinel instead.
_UNSPECIFIED_SOURCE = "__unspecified__"


def _chunks(rows: list[dict], size: int = _INSERT_CHUNK_ROWS):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    return _engine


_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
# Separate key from MONITOR_LOCK_KEY: serialises schema upgrades when several API workers or
# replicas start at once.
_MIGRATION_LOCK_KEY = 0x4D41494D4947  # "MAIMIG"


def init_db() -> None:
    """Bring the schema to the latest Alembic revision.

    The table definitions in this module describe the schema for queries; the schema itself is
    owned by the revisions in migrations/versions. A blocking advisory lock makes concurrent
    startups wait for one upgrade instead of racing each other.
    """
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    with engine().connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY})
        conn.commit()
        try:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            conn.commit()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY})
            conn.commit()


def check_database() -> bool:
    with engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return True

_SOURCE_ORIGINS = {
    "api_push",
    "document",
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
            source_id = _UNSPECIFIED_SOURCE
            payload.pop("source_id", None)
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
        for chunk in _chunks(telemetry_rows):
            statement = pg_insert(telemetry).values(chunk)
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
            UNION SELECT well_id FROM analysis_runs
            UNION SELECT well_id FROM source_health
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
        source_id = _UNSPECIFIED_SOURCE
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
                for chunk in _chunks(event_rows):
                    # rowcount is not reliable for multi-row INSERT ... ON CONFLICT DO NOTHING
                    # with this driver, so count the rows actually inserted via RETURNING.
                    statement = pg_insert(history_events).values(chunk).on_conflict_do_nothing(
                        index_elements=[history_events.c.well_id, history_events.c.source_id,
                                        history_events.c.event_type, history_events.c.event_fingerprint]
                    ).returning(history_events.c.id)
                    inserted_counts[event_type] += len(conn.execute(statement).all())
    return {"batch_id": batch_id, "well_id": well_id,
            "work_records_received": len(work_history), "failure_records_received": len(failure_history),
            "work_records_stored": inserted_counts["work"],
            "failure_records_stored": inserted_counts["failure"], "idempotent_replay": False}


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


def save_analysis_run(result: PipelineResult | dict, request_id: str | None = None,
                      horizon_days: int | None = None) -> dict:
    payload = result.model_dump(mode="json") if isinstance(result, PipelineResult) else result
    analysis_id = payload["analysis_id"]
    if horizon_days is None:
        horizon_days = payload.get("horizon_days") or next(
            (item.get("horizon_days") for item in payload.get("model_predictions", [])), None)
    model_versions = {item["model_id"]: item["model_version"]
                      for item in payload.get("model_predictions", [])}
    created_at = datetime.now(timezone.utc)
    with engine().begin() as conn:
        conn.execute(analysis_runs.insert().values(
            analysis_id=analysis_id, well_id=payload["well_id"], request_id=request_id,
            created_at=created_at, status=payload["status"], model_versions_json=model_versions,
            result_json=payload, horizon_days=horizon_days,
        ))
    return {"analysis_id": analysis_id, "well_id": payload["well_id"],
            "created_at": created_at.isoformat(), "status": payload["status"],
            "model_versions": model_versions, "request_id": request_id, "horizon_days": horizon_days}


def save_agent_result(analysis_id: str, well_id: str, agent_status: str,
                      agent_response: dict | None, external_data: dict | None) -> bool:
    """Store the AI report on its run and on sibling horizon runs from the same request.

    A scheduled monitor run analyses each well on several horizons under one request_id but
    requests a single AI report per well; the sibling runs get the same report so every
    horizon row on the dashboard shows it, without paying for one LLM call per horizon.
    """
    values = dict(agent_status=agent_status, agent_updated_at=datetime.now(timezone.utc),
                  agent_response_json=agent_response, external_data_json=external_data)
    with engine().begin() as conn:
        result = conn.execute(analysis_runs.update().where(
            analysis_runs.c.analysis_id == analysis_id,
            analysis_runs.c.well_id == well_id,
        ).values(**values))
        if result.rowcount != 1:
            return False
        request_id = conn.execute(select(analysis_runs.c.request_id).where(
            analysis_runs.c.analysis_id == analysis_id)).scalar()
        if request_id:
            conn.execute(analysis_runs.update().where(
                analysis_runs.c.well_id == well_id,
                analysis_runs.c.request_id == request_id,
                analysis_runs.c.analysis_id != analysis_id,
                analysis_runs.c.agent_status.is_(None),
            ).values(**values))
        return True


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
    """Latest run for every (well_id, horizon_days) pair, read with one indexed query."""
    query = (
        select(analysis_runs)
        .distinct(analysis_runs.c.well_id, analysis_runs.c.horizon_days)
        .order_by(analysis_runs.c.well_id, analysis_runs.c.horizon_days,
                  analysis_runs.c.created_at.desc())
    )
    with engine().connect() as conn:
        rows = conn.execute(query).mappings().all()
    return [{
        "analysis_id": row["analysis_id"], "well_id": row["well_id"],
        "horizon_days": row["horizon_days"],
        "created_at": row["created_at"].isoformat(), "status": row["status"],
        "analysis": row["result_json"], "agent_response": row["agent_response_json"],
        "agent_status": row["agent_status"],
        "agent_updated_at": row["agent_updated_at"].isoformat() if row["agent_updated_at"] else None,
    } for row in rows]


def prune_analysis_runs(retention_days: int) -> int:
    """Delete runs older than the retention window, always keeping the latest run per
    (well_id, horizon_days) so the dashboard never loses a well. Returns rows deleted."""
    if retention_days <= 0:
        return 0
    statement = text("""
        DELETE FROM analysis_runs a
        WHERE a.created_at < now() - make_interval(days => :days)
          AND a.analysis_id NOT IN (
              SELECT DISTINCT ON (well_id, horizon_days) analysis_id
              FROM analysis_runs
              ORDER BY well_id, horizon_days, created_at DESC
          )
    """)
    with engine().begin() as conn:
        return conn.execute(statement, {"days": retention_days}).rowcount or 0

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


# Arbitrary application-wide key for pg_try_advisory_lock: at most one monitor run at a time
# across all API workers and replicas sharing the database.
MONITOR_LOCK_KEY = 0x4D41494D4F4E  # "MAIMON"


@contextmanager
def advisory_lock(key: int) -> Iterator[bool]:
    """Try to take a session-level advisory lock; yields whether it was acquired."""
    with engine().connect() as conn:
        acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
        conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()


def create_monitor_job(job_id: str, horizon_days: int) -> dict:
    row = {"job_id": job_id, "status": "queued", "horizon_days": horizon_days,
           "created_at": datetime.now(timezone.utc)}
    with engine().begin() as conn:
        conn.execute(monitor_jobs.insert().values(**row))
    return get_monitor_job(job_id)


def update_monitor_job(job_id: str, **values) -> None:
    with engine().begin() as conn:
        conn.execute(monitor_jobs.update().where(monitor_jobs.c.job_id == job_id).values(**values))


def get_monitor_job(job_id: str) -> dict | None:
    with engine().connect() as conn:
        row = conn.execute(select(monitor_jobs).where(monitor_jobs.c.job_id == job_id)).mappings().first()
    if row is None:
        return None
    iso = lambda value: value.isoformat() if value else None  # noqa: E731
    return {"job_id": row["job_id"], "status": row["status"], "horizon_days": row["horizon_days"],
            "created_at": iso(row["created_at"]), "started_at": iso(row["started_at"]),
            "completed_at": iso(row["completed_at"]), "result": row["result_json"],
            "error": row["error"]}


# --------------------------------------------------------------------------- well rating
# Computed by app.core.rating_builder; never edited by hand.
COMPUTED_RATING_FIELDS = (
    "field", "equipment", "in_operation", "sampling", "corrosive_environment", "tonnage_t",
    "noo_days", "demount_act", "demount_corrosion", "base_act", "base_corrosion", "category",
    "model_ntc", "model_math", "model_ntc_raw", "protection",
    "installed_at", "liquid_rate_m3_day", "oil_pct", "data_gaps",
)
# Edited by the operator on the dashboard; a recomputation never touches them.
OPERATOR_RATING_FIELDS = ("plan_date", "promine_status", "comment")


def _rating_row(row) -> dict:
    data = dict(row)
    for key in ("assessed_at", "updated_at", "measure_since", "computed_at", "installed_at", "ai_proposal_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    if data.get("plan_date") is not None:
        data["plan_date"] = data["plan_date"].isoformat()
    return data


def save_computed_rating(well_id: str, values: dict, *, measure: str | None, provenance: dict,
                         skip_reason: str | None, analysis_id: str | None) -> dict:
    """Store a well's computed rating, keeping the operator fields.

    measure_since (the start of the measure deadline) moves only when the measure changes, so
    the daily recomputation does not keep pushing deadlines forward.
    """
    now = datetime.now(timezone.utc)
    computed = {key: values.get(key) for key in COMPUTED_RATING_FIELDS}
    computed.update(measure=measure, provenance=provenance, skip_reason=skip_reason,
                    analysis_id=analysis_id, computed_at=now, assessed_at=now, updated_at=now,
                    source="computed")
    with engine().begin() as conn:
        old = conn.execute(select(well_rating.c.measure, well_rating.c.measure_since)
                           .where(well_rating.c.well_id == well_id).with_for_update()).mappings().first()
        if old is None:
            computed["measure_since"] = now if measure else None
            conn.execute(well_rating.insert().values(well_id=well_id, **computed))
        else:
            if old["measure"] != measure or old["measure_since"] is None:
                computed["measure_since"] = now if measure else None
            conn.execute(well_rating.update().where(well_rating.c.well_id == well_id).values(**computed))
        row = conn.execute(select(well_rating).where(well_rating.c.well_id == well_id)).mappings().one()
    return _rating_row(row)


def list_rating_rows() -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(select(well_rating)).mappings().all()
    return [_rating_row(row) for row in rows]


def get_rating_row(well_id: str) -> dict | None:
    with engine().connect() as conn:
        row = conn.execute(select(well_rating).where(well_rating.c.well_id == well_id)).mappings().first()
    return _rating_row(row) if row else None


def rating_scores() -> list[tuple[str, float | None, float | None]]:
    """(well_id, model_ntc, model_math) of every well: enough to rank one well without loading all rows."""
    with engine().connect() as conn:
        return [tuple(row) for row in conn.execute(
            select(well_rating.c.well_id, well_rating.c.model_ntc, well_rating.c.model_math)).all()]


def update_rating_row(well_id: str, values: dict) -> bool:
    """Change operator fields only (plan, Промайн document status, comment)."""
    values = {key: value for key, value in values.items() if key in OPERATOR_RATING_FIELDS}
    with engine().begin() as conn:
        result = conn.execute(well_rating.update().where(well_rating.c.well_id == well_id).values(
            **values, updated_at=datetime.now(timezone.utc)))
        return result.rowcount == 1


# --------------------------------------------------------------------------- ERA proposals
def _proposal_row(row) -> dict:
    data = dict(row)
    for key in ("created_at", "sent_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    if data.get("proposed_date") is not None:
        data["proposed_date"] = data["proposed_date"].isoformat()
    return data


def create_era_proposal(values: dict) -> dict:
    with engine().begin() as conn:
        conn.execute(era_proposals.insert().values(**values))
    return get_era_proposal(values["proposal_id"])


def update_era_proposal(proposal_id: str, **values) -> None:
    with engine().begin() as conn:
        conn.execute(era_proposals.update().where(era_proposals.c.proposal_id == proposal_id).values(**values))


def get_era_proposal(proposal_id: str) -> dict | None:
    with engine().connect() as conn:
        row = conn.execute(select(era_proposals).where(
            era_proposals.c.proposal_id == proposal_id)).mappings().first()
    return _proposal_row(row) if row else None


def list_era_proposals(well_id: str | None = None, limit: int = 100) -> list[dict]:
    query = select(era_proposals).order_by(era_proposals.c.created_at.desc()).limit(limit)
    if well_id is not None:
        query = query.where(era_proposals.c.well_id == well_id)
    with engine().connect() as conn:
        return [_proposal_row(row) for row in conn.execute(query).mappings().all()]


def latest_era_proposal(well_id: str) -> dict | None:
    """The well's most recent proposal, with the same fields as latest_era_proposals()."""
    query = (select(era_proposals.c.well_id, era_proposals.c.proposal_id, era_proposals.c.status,
                    era_proposals.c.change_type, era_proposals.c.created_at)
             .where(era_proposals.c.well_id == well_id).order_by(era_proposals.c.created_at.desc()).limit(1))
    with engine().connect() as conn:
        row = conn.execute(query).mappings().first()
    return _proposal_row(row) if row else None


def latest_era_proposals() -> dict[str, dict]:
    """Most recent proposal per well, for the rating table."""
    query = (select(era_proposals.c.well_id, era_proposals.c.proposal_id, era_proposals.c.status,
                    era_proposals.c.change_type, era_proposals.c.created_at)
             .distinct(era_proposals.c.well_id)
             .order_by(era_proposals.c.well_id, era_proposals.c.created_at.desc()))
    with engine().connect() as conn:
        return {row["well_id"]: _proposal_row(row) for row in conn.execute(query).mappings().all()}


# --------------------------------------------------------------------------- documents
DOCUMENT_LIST_COLUMNS = [column for column in documents.c if column.name != "text"]
# The list leaves out the extracted records too: a registry of many wells makes them large.
DOCUMENT_SUMMARY_COLUMNS = [column for column in DOCUMENT_LIST_COLUMNS if column.name != "extraction"]


def _document_row(row, with_text: bool = False) -> dict:
    data = dict(row)
    for key in ("created_at", "started_at", "processed_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    if data.get("doc_date") is not None:
        data["doc_date"] = data["doc_date"].isoformat()
    if not with_text:
        data.pop("text", None)
    return data


def create_document(values: dict) -> tuple[dict, bool]:
    """Insert a document row; a file with the same SHA-256 returns the existing row instead.

    Returns (row, created)."""
    with engine().begin() as conn:
        inserted = conn.execute(pg_insert(documents).values(**values).on_conflict_do_nothing(
            index_elements=[documents.c.sha256]).returning(documents.c.document_id)).first()
        row = conn.execute(select(*DOCUMENT_LIST_COLUMNS).where(
            documents.c.sha256 == values["sha256"])).mappings().one()
    return _document_row(row), inserted is not None


def get_document(document_id: str, with_text: bool = False) -> dict | None:
    columns = list(documents.c) if with_text else DOCUMENT_LIST_COLUMNS
    with engine().connect() as conn:
        row = conn.execute(select(*columns).where(documents.c.document_id == document_id)).mappings().first()
    return _document_row(row, with_text) if row else None


def list_documents(*, well_id: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
    query = select(*DOCUMENT_SUMMARY_COLUMNS).order_by(documents.c.created_at.desc()).limit(limit)
    if status:
        query = query.where(documents.c.status == status)
    if well_id:
        query = query.where(documents.c.well_ids.cast(JSONB).contains([well_id]))
    with engine().connect() as conn:
        return [_document_row(row) for row in conn.execute(query).mappings().all()]


def document_status_counts() -> dict[str, int]:
    with engine().connect() as conn:
        rows = conn.execute(text("SELECT status, COUNT(*) FROM documents GROUP BY status")).all()
    return {status: count for status, count in rows}


def update_document(document_id: str, **values) -> None:
    with engine().begin() as conn:
        conn.execute(update(documents).where(documents.c.document_id == document_id).values(**values))


def claim_next_document(document_id: str | None = None) -> dict | None:
    """Mark the oldest queued document (or the given one) as processing and return it; safe
    across workers. A document returned to the queue after a failed attempt waits
    MAI_DOCS_RETRY_MINUTES before the next automatic attempt."""
    statement = text("""
        UPDATE documents SET status = 'processing', started_at = :now, attempts = attempts + 1
        WHERE document_id = (
            SELECT document_id FROM documents
            WHERE status = 'queued'
              AND (CAST(:document_id AS varchar) IS NULL OR document_id = :document_id)
              AND (CAST(:document_id AS varchar) IS NOT NULL OR started_at IS NULL
                   OR started_at < :now - make_interval(mins => :retry))
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING document_id
    """)
    with engine().begin() as conn:
        row = conn.execute(statement, {"now": datetime.now(timezone.utc), "document_id": document_id,
                                       "retry": settings.docs_retry_minutes}).first()
    return get_document(row[0]) if row else None


def requeue_stale_documents(older_than_minutes: int = 30) -> int:
    """Documents left in processing by a stopped worker go back to the queue."""
    with engine().begin() as conn:
        result = conn.execute(text("""
            UPDATE documents SET status = 'queued'
            WHERE status = 'processing' AND started_at < now() - make_interval(mins => :mins)
        """), {"mins": older_than_minutes})
    return result.rowcount or 0


def delete_document_data(document_id: str, extra_batch_ids: Iterable[str] = ()) -> None:
    """Remove the telemetry, history and passport rows loaded from one document (and the
    batches of a source import made from it, when given)."""
    prefix = f"doc-{document_id}-%"
    extra = [item for item in extra_batch_ids if item]
    with engine().begin() as conn:
        conn.execute(delete(ingestion_batches).where(ingestion_batches.c.batch_id.like(prefix)))
        conn.execute(delete(history_batches).where(history_batches.c.batch_id.like(prefix)))
        if extra:
            conn.execute(delete(ingestion_batches).where(ingestion_batches.c.batch_id.in_(extra)))
            conn.execute(delete(history_batches).where(history_batches.c.batch_id.in_(extra)))
        conn.execute(text("""
            DELETE FROM source_documents
            WHERE (CAST(payload_json AS jsonb) ->> 'document_id') = :document_id
        """), {"document_id": document_id})


def delete_document_row(document_id: str) -> None:
    with engine().begin() as conn:
        conn.execute(delete(documents).where(documents.c.document_id == document_id))


def load_extra_measurements(well_id: str) -> list[dict]:
    """Rows with additional indicators (pH, hardness…) of a well, oldest first."""
    query = text("""
        SELECT source_id, timestamp, CAST(payload_json AS jsonb) -> 'extra_measurements' AS extras
        FROM telemetry
        WHERE well_id = :well_id
          AND jsonb_typeof(CAST(payload_json AS jsonb) -> 'extra_measurements') = 'object'
        ORDER BY timestamp, id
    """)
    with engine().connect() as conn:
        rows = conn.execute(query, {"well_id": well_id}).mappings().all()
    return [{"source_id": None if row["source_id"] == _UNSPECIFIED_SOURCE else row["source_id"],
             "timestamp": row["timestamp"], "extras": row["extras"]} for row in rows]


def well_has_data(well_id: str) -> bool:
    """Whether any source data of the well remain: telemetry, history events or source documents."""
    with engine().connect() as conn:
        for table in (telemetry, history_events, source_documents):
            if conn.execute(select(table.c.well_id).where(table.c.well_id == well_id).limit(1)).first():
                return True
    return False


def forget_well(well_id: str) -> None:
    """Remove what was derived for a well that has no data left (its rating row, analyses and
    source statuses), e.g. a well a deleted document had created."""
    with engine().begin() as conn:
        for table in (well_rating, analysis_runs, source_health):
            conn.execute(delete(table).where(table.c.well_id == well_id))


def known_well_ids() -> list[str]:
    """Wells the system knows from any source, the rating or MAI_WELL_IDS."""
    query = text("""
        SELECT well_id FROM telemetry UNION SELECT well_id FROM history_events
        UNION SELECT well_id FROM well_rating UNION SELECT well_id FROM source_health
    """)
    with engine().connect() as conn:
        wells = {row[0] for row in conn.execute(query).all()}
    return sorted(wells | set(settings.configured_well_ids))


def save_ai_proposal(well_id: str, proposal: dict) -> bool:
    with engine().begin() as conn:
        result = conn.execute(well_rating.update().where(well_rating.c.well_id == well_id).values(
            ai_proposal=proposal, ai_proposal_at=datetime.now(timezone.utc)))
        return result.rowcount == 1
