"""Database of the service: Microsoft SQL Server 2016+ (compatibility level 130+ for the JSON functions).

MAI_DATABASE_URL connects through pyodbc and an installed "ODBC Driver 17/18 for SQL Server"
(mssql+pyodbc://…?driver=…) or through pymssql, which needs no ODBC driver (mssql+pymssql://…);
MAI_DATABASE_SCHEMA names the schema of the tables (empty: the default schema of the login, usually
dbo). At start (init_db) the tables are created from the definitions below, columns a later version
defines are added to existing tables, and tables and columns of earlier versions are dropped.

Text is NVARCHAR with a binary collation, so keys compare exactly whatever the collation of the
database (DEMO-1 and demo-1 stay two wells); moments are DATETIME2 in UTC, read back with tzinfo=UTC;
JSON is NVARCHAR(MAX).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

from sqlalchemy import (BigInteger, Boolean, Column, Date, Float, ForeignKey, Index, Integer, JSON, MetaData, Table,
                        Unicode, UnicodeText, UniqueConstraint, bindparam, create_engine, delete, func, inspect,
                        literal_column, select, text, union, update)
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateColumn
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app.schemas import FailureHistoryItem, TelemetryPoint, WorkHistoryItem

# Exact comparison and ordering of text, independent of the collation of the database.
BINARY_COLLATION = "Latin1_General_100_BIN2"
# SQL Server accepts at most 2100 parameters in one request and 1000 rows in one VALUES list.
MAX_PARAMETERS = 2000
MAX_VALUES_ROWS = 1000
# Wait for the schema lock of another starting process (milliseconds).
SCHEMA_LOCK_TIMEOUT_MS = 600_000
# Tables and columns of earlier versions, dropped at start: a NOT NULL column the application no
# longer writes would make its inserts fail.
OBSOLETE_TABLES = ("analysis_runs", "era_proposals")
OBSOLETE_COLUMNS = {"monitor_jobs": ("horizon_days",),
                    "well_rating": ("model_math", "skip_reason", "analysis_id")}
# Unique constraints of SQL Server let one NULL in; rows without a valid source are keyed under this
# sentinel, so identical rows are never stored twice.
_UNSPECIFIED_SOURCE = "__unspecified__"


class UTCDateTime(TypeDecorator):
    """A moment stored as UTC in DATETIME2 and returned with tzinfo=UTC (naive values are UTC)."""

    impl = DATETIME2
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def NVarchar(length: int | None = None):  # noqa: N802  (a column type, named like the SQLAlchemy ones)
    """NVARCHAR(length); NVARCHAR(MAX) without a length or beyond 4000; binary collation."""
    if length and length <= 4000:
        return Unicode(length, collation=BINARY_COLLATION)
    return UnicodeText(collation=BINARY_COLLATION)

metadata = MetaData()
ingestion_batches = Table(
    "ingestion_batches", metadata,
    Column("batch_id", NVarchar(128), primary_key=True), Column("well_id", NVarchar(128), nullable=False),
    Column("record_count", Integer, nullable=False), Column("created_at", UTCDateTime, nullable=False),
    Column("payload_sha256", NVarchar(64), nullable=False),
)
telemetry = Table(
    "telemetry", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("batch_id", NVarchar(128), ForeignKey("ingestion_batches.batch_id", ondelete="CASCADE"), nullable=False),
    Column("well_id", NVarchar(128), nullable=False), Column("source_id", NVarchar(128)),
    Column("source_occurrence", Integer), Column("record_index", Integer, nullable=False),
    Column("timestamp", UTCDateTime, nullable=False), Column("payload_json", JSON, nullable=False),
    UniqueConstraint("batch_id", "record_index", name="uq_telemetry_batch_record"),
    UniqueConstraint("well_id", "source_id", "timestamp", "source_occurrence",
                     name="uq_telemetry_source_observation"),
)
Index("idx_telemetry_well_time", telemetry.c.well_id, telemetry.c.timestamp)
history_batches = Table(
    "history_batches", metadata,
    Column("batch_id", NVarchar(128), primary_key=True), Column("well_id", NVarchar(128), nullable=False),
    Column("work_count", Integer, nullable=False), Column("failure_count", Integer, nullable=False),
    Column("payload_sha256", NVarchar(64), nullable=False), Column("created_at", UTCDateTime, nullable=False),
)
history_events = Table(
    "history_events", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("batch_id", NVarchar(128), ForeignKey("history_batches.batch_id", ondelete="CASCADE"), nullable=False),
    Column("well_id", NVarchar(128), nullable=False), Column("source_id", NVarchar(128)),
    Column("event_type", NVarchar(16), nullable=False), Column("event_fingerprint", NVarchar(64), nullable=False),
    Column("record_index", Integer, nullable=False), Column("event_date", UTCDateTime, nullable=False),
    Column("payload_json", JSON, nullable=False),
    UniqueConstraint("batch_id", "event_type", "record_index", name="uq_history_batch_event"),
    UniqueConstraint("well_id", "source_id", "event_type", "event_fingerprint",
                     name="uq_history_source_event"),
)
Index("idx_history_well_time", history_events.c.well_id, history_events.c.event_date)
source_health = Table(
    "source_health", metadata,
    Column("source_id", NVarchar(128), primary_key=True), Column("well_id", NVarchar(128), primary_key=True),
    Column("origin", NVarchar(32), primary_key=True, nullable=False, server_default="api_refresh"),
    Column("status", NVarchar(32), nullable=False), Column("message", NVarchar(2000)),
    Column("record_count", Integer), Column("retrieved_at", UTCDateTime, nullable=False),
)
monitor_jobs = Table(
    "monitor_jobs", metadata,
    Column("job_id", NVarchar(64), primary_key=True), Column("status", NVarchar(32), nullable=False),
    Column("created_at", UTCDateTime, nullable=False), Column("started_at", UTCDateTime), Column("completed_at", UTCDateTime),
    Column("result_json", JSON), Column("error", NVarchar(2000)),
)
# Current rating per well. Evidence and the НТЦ score are computed from source data by
# app.core.rating_builder (provenance records the source and date of each value); plan_date,
# promine_status and comment are operator fields (PATCH /rating/wells/{id}) never overwritten by a
# recomputation. The index, measure and deadline are derived in app.core.rating.
well_rating = Table(
    "well_rating", metadata,
    Column("well_id", NVarchar(128), primary_key=True),
    Column("field", NVarchar(256)), Column("equipment", NVarchar(256)),
    Column("in_operation", Boolean), Column("sampling", Boolean),
    Column("corrosive_environment", Boolean),
    Column("tonnage_t", Float), Column("noo_days", Float),
    Column("demount_act", Boolean), Column("demount_corrosion", Boolean),
    Column("base_act", Boolean), Column("base_corrosion", Boolean),
    Column("category", NVarchar(32)),
    Column("model_ntc", Float),
    Column("plan_date", Date), Column("promine_status", NVarchar(32)),
    Column("comment", NVarchar(2000)),
    Column("assessed_at", UTCDateTime),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("source", NVarchar(32), nullable=False),
    Column("measure", NVarchar(32)), Column("measure_since", UTCDateTime),
    Column("provenance", JSON), Column("computed_at", UTCDateTime),
    Column("protection", NVarchar(32)), Column("model_ntc_raw", Float),
    Column("installed_at", UTCDateTime), Column("liquid_rate_m3_day", Float),
    Column("oil_pct", Float), Column("data_gaps", JSON),
    # Latest proposal drafted by the AI agent from all well data (app.core.proposal_agent).
    Column("ai_proposal", JSON), Column("ai_proposal_at", UTCDateTime),
)
source_documents = Table(
    "source_documents", metadata,
    Column("source_id", NVarchar(128), primary_key=True), Column("well_id", NVarchar(128), primary_key=True),
    Column("payload_json", JSON, nullable=False), Column("updated_at", UTCDateTime, nullable=False),
)
# Documents received as files (manual upload or the inbox folder): the original is stored on
# disk, the extracted text and fields here. Data taken from a document is written to the usual
# telemetry / history tables under batch ids "doc-<document_id>-<well_id>".
documents = Table(
    "documents", metadata,
    Column("document_id", NVarchar(36), primary_key=True),
    Column("sha256", NVarchar(64), nullable=False, unique=True),
    Column("filename", NVarchar(512), nullable=False),
    Column("channel", NVarchar(16), nullable=False),
    Column("file_kind", NVarchar(16), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("storage_path", NVarchar(1024), nullable=False),
    Column("status", NVarchar(32), nullable=False),
    Column("status_message", NVarchar(2000)),
    Column("pages", Integer),
    Column("doc_type", NVarchar(64)),
    Column("well_ids", JSON),
    Column("doc_date", Date),
    Column("text_method", NVarchar(32)),
    Column("text_chars", Integer),
    Column("text", NVarchar()),
    Column("extraction", JSON),
    Column("loaded", JSON),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("created_at", UTCDateTime, nullable=False),
    Column("started_at", UTCDateTime),
    Column("processed_at", UTCDateTime),
)
Index("idx_documents_status_created", documents.c.status, documents.c.created_at)
Index("idx_documents_created", documents.c.created_at)


# --------------------------------------------------------------------------- connection
_engine: Engine | None = None


def connect_args(url: str, login_timeout: int = 30) -> dict:
    """Driver options: a login timeout (pymssql's "timeout" limits every query, so its own option);
    pymssql binds DATETIME2 with full precision, as pyodbc does."""
    if url.startswith("mssql+pymssql"):
        return {"login_timeout": login_timeout, "use_datetime2": True}
    return {"timeout": login_timeout}


def engine() -> Engine:
    global _engine
    if _engine is None:
        url = settings.database_url
        if not url.startswith("mssql"):
            raise RuntimeError("MAI_DATABASE_URL is not a SQL Server address (mssql+pyodbc:// or mssql+pymssql://)")
        _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10, connect_args=connect_args(url))
        if settings.database_schema:
            _engine = _engine.execution_options(schema_translate_map={None: settings.database_schema})
    return _engine


def qualified(name: str) -> str:
    """A table name for hand-written SQL, in the configured schema."""
    quote = engine().dialect.identifier_preparer.quote
    return (quote(settings.database_schema) + "." if settings.database_schema else "") + quote(name)


def _applock(conn, resource: str, timeout_ms: int) -> bool:
    # NOCOUNT only around the call: rowcount of later statements on this connection must stay.
    result = conn.execute(text(
        "SET NOCOUNT ON; DECLARE @result int; "
        "EXEC @result = sp_getapplock @Resource = :resource, @LockMode = 'Exclusive', "
        "@LockOwner = 'Session', @LockTimeout = :timeout; "
        "SET NOCOUNT OFF; SELECT @result"), {"resource": resource, "timeout": timeout_ms}).scalar()
    return result is not None and result >= 0


def _release(conn, resource: str) -> None:
    conn.execute(text("EXEC sp_releaseapplock @Resource = :resource, @LockOwner = 'Session'"), {"resource": resource})


def _lock_name(key: str) -> str:
    return f"mai:{settings.database_schema or 'default'}:{key}"


def init_db() -> None:
    """Create the schema and the tables; add the columns new versions define, drop obsolete ones.
    An application lock makes processes starting at once wait for one another."""
    schema = settings.database_schema
    with engine().connect() as conn:
        if schema:
            conn.execute(text("DECLARE @sql nvarchar(400) = N'CREATE SCHEMA ' + QUOTENAME(:schema); "
                              "IF SCHEMA_ID(:schema) IS NULL EXEC sp_executesql @sql"), {"schema": schema})
            conn.commit()
        resource = _lock_name("schema")
        if not _applock(conn, resource, SCHEMA_LOCK_TIMEOUT_MS):
            raise RuntimeError("Таблицы создаёт другой процесс; повторите позже.")
        conn.commit()
        try:
            metadata.create_all(conn)
            inspector = inspect(conn)
            quote = conn.dialect.identifier_preparer.quote
            for name in OBSOLETE_TABLES:
                if inspector.has_table(name, schema=schema or None):
                    conn.execute(text(f"DROP TABLE {qualified(name)}"))
            for table in metadata.sorted_tables:
                existing = {column["name"].lower() for column in inspector.get_columns(table.name, schema=schema or None)}
                for column in table.columns:
                    if column.name.lower() not in existing:
                        ddl = CreateColumn(column).compile(dialect=conn.dialect)
                        conn.execute(text(f"ALTER TABLE {qualified(table.name)} ADD {ddl}"))
                for name in OBSOLETE_COLUMNS.get(table.name, ()):
                    if name in existing:
                        conn.execute(text(f"ALTER TABLE {qualified(table.name)} DROP COLUMN {quote(name)}"))
            conn.commit()
        finally:
            _release(conn, resource)
            conn.commit()


def check_database() -> bool:
    with engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return True


# Any key of this application: at most one daily cycle at a time across all processes of the database.
MONITOR_LOCK_KEY = "monitor"


@contextmanager
def advisory_lock(key) -> Iterator[bool]:
    """Try to take an application lock of this database; yields whether it was acquired."""
    resource = _lock_name(str(key))
    with engine().connect() as conn:
        acquired = _applock(conn, resource, 0)
        conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                _release(conn, resource)
                conn.commit()


# --------------------------------------------------------------------------- insert or update (MERGE)
class _Merge(Executable, ClauseElement):
    """Insert the rows whose key is new; for the others update the given columns from the row
    (MERGE … WITH (HOLDLOCK)). Rows proposing the same key: the first one wins. With output, the
    statement returns that column of the inserted rows."""

    inherit_cache = False
    _execution_options = Executable._execution_options

    def __init__(self, table, rows: list[dict], keys: tuple[str, ...], update: tuple[str, ...] = (),
                 output: str | None = None) -> None:
        self.table, self.keys, self.update, self.output = table, keys, update, output
        seen: set[tuple] = set()
        self.rows = []
        for row in rows:
            key = tuple(row.get(name) for name in keys)
            if key not in seen:
                seen.add(key)
                self.rows.append(row)


@compiles(_Merge, "mssql")
def _compile_merge(element: _Merge, compiler, **kw) -> str:
    table, quote = element.table, compiler.preparer.quote
    columns = list(element.rows[0])
    values = []
    for index, row in enumerate(element.rows):
        cells = [compiler.process(bindparam(f"m{index}_{position}", row.get(name), type_=table.c[name].type,
                                            unique=True), **kw) for position, name in enumerate(columns)]
        values.append("(" + ", ".join(cells) + ")")
    source = ", ".join(quote(name) for name in columns)
    on = " AND ".join(f"t.{quote(name)} = s.{quote(name)}" for name in element.keys)
    sql = (f"MERGE {compiler.preparer.format_table(table)} WITH (HOLDLOCK) AS t "
           f"USING (VALUES {', '.join(values)}) AS s ({source}) ON {on}")
    if element.update:
        sql += " WHEN MATCHED THEN UPDATE SET " + ", ".join(f"t.{quote(name)} = s.{quote(name)}"
                                                           for name in element.update)
    sql += f" WHEN NOT MATCHED THEN INSERT ({source}) VALUES (" + ", ".join(f"s.{quote(name)}" for name in columns) + ")"
    if element.output:
        sql += f" OUTPUT inserted.{quote(element.output)}"
    return sql + ";"


def _merge(conn, table, rows: list[dict], keys: tuple[str, ...], update: tuple[str, ...] = (),
           output: str | None = None) -> int | list:
    """Run _Merge in chunks within the parameter limits; the inserted rows' output values with
    output, else the number of rows inserted or updated."""
    width = max(1, len(rows[0])) if rows else 1
    size = max(1, min(MAX_VALUES_ROWS, MAX_PARAMETERS // width))
    count, returned = 0, []
    for start in range(0, len(rows), size):
        result = conn.execute(_Merge(table, rows[start:start + size], keys, update, output))
        if output:
            returned += [row[0] for row in result.all()]
        else:
            count += result.rowcount or 0
    return returned if output else count


# --------------------------------------------------------------------------- source data
_SOURCE_ORIGINS = {"api_push", "document", "manual_upload", "api_refresh", "adapter", "system"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _json_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def save_batch(well_id: str, requested_batch_id: str | None, records: Iterable[TelemetryPoint]) -> dict:
    """Store telemetry; a value at a moment already stored for the source replaces the old one.
    A batch id used again with the same well and payload is an idempotent replay."""
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
    with engine().begin() as conn:
        inserted = _merge(conn, ingestion_batches, [{
            "batch_id": batch_id, "well_id": well_id, "record_count": len(rows),
            "created_at": datetime.now(timezone.utc), "payload_sha256": digest}], ("batch_id",))
        if not inserted:
            existing = conn.execute(select(ingestion_batches).where(
                ingestion_batches.c.batch_id == batch_id)).mappings().one()
            if existing["well_id"] != well_id or existing["payload_sha256"] != digest:
                raise ValueError("batch_id has already been used with a different well or payload")
            return {"batch_id": batch_id, "well_id": well_id, "records_received": len(rows),
                    "records_stored": existing["record_count"], "idempotent_replay": True}
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


def save_history(well_id: str, requested_batch_id: str | None,
                 work_history: list[WorkHistoryItem], failure_history: list[FailureHistoryItem],
                 source_id: str | None = None) -> dict:
    """Store work and failure events; an event already stored for the source is skipped."""
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
    with engine().begin() as conn:
        inserted = _merge(conn, history_batches, [{
            "batch_id": batch_id, "well_id": well_id, "work_count": len(work_history),
            "failure_count": len(failure_history), "payload_sha256": digest,
            "created_at": datetime.now(timezone.utc)}], ("batch_id",))
        if not inserted:
            existing = conn.execute(select(history_batches).where(
                history_batches.c.batch_id == batch_id)).mappings().one()
            if existing["well_id"] != well_id or existing["payload_sha256"] != digest:
                raise ValueError("history batch_id has already been used with a different well or payload")
            return {"batch_id": batch_id, "well_id": well_id,
                    "work_records_received": len(work_history), "failure_records_received": len(failure_history),
                    "work_records_stored": existing["work_count"],
                    "failure_records_stored": existing["failure_count"], "idempotent_replay": True}
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


def forget_well(well_id: str) -> None:
    """Remove what was derived for a well that has no data left (its rating row and source
    statuses), e.g. a well a deleted document had created."""
    with engine().begin() as conn:
        for table in (well_rating, source_health):
            conn.execute(delete(table).where(table.c.well_id == well_id))


# --------------------------------------------------------------------------- daily cycle jobs
def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def create_monitor_job(job_id: str) -> dict:
    with engine().begin() as conn:
        conn.execute(monitor_jobs.insert().values(job_id=job_id, status="queued", created_at=datetime.now(timezone.utc)))
    return get_monitor_job(job_id)


def update_monitor_job(job_id: str, **values) -> None:
    with engine().begin() as conn:
        conn.execute(monitor_jobs.update().where(monitor_jobs.c.job_id == job_id).values(**values))


def get_monitor_job(job_id: str) -> dict | None:
    with engine().connect() as conn:
        row = conn.execute(select(monitor_jobs).where(monitor_jobs.c.job_id == job_id)).mappings().first()
    if row is None:
        return None
    return {"job_id": row["job_id"], "status": row["status"], "created_at": _iso(row["created_at"]),
            "started_at": _iso(row["started_at"]), "completed_at": _iso(row["completed_at"]),
            "result": row["result_json"], "error": row["error"]}


# --------------------------------------------------------------------------- well rating
# Computed by app.core.rating_builder; never edited by hand.
COMPUTED_RATING_FIELDS = (
    "field", "equipment", "in_operation", "sampling", "corrosive_environment", "tonnage_t",
    "noo_days", "demount_act", "demount_corrosion", "base_act", "base_corrosion", "category",
    "model_ntc", "model_ntc_raw", "protection", "installed_at", "liquid_rate_m3_day", "oil_pct", "data_gaps",
)
# Edited by the operator; a recomputation never touches them.
OPERATOR_RATING_FIELDS = ("plan_date", "promine_status", "comment")


def _rating_row(row) -> dict:
    data = dict(row)
    for key in ("assessed_at", "updated_at", "measure_since", "computed_at", "installed_at", "ai_proposal_at",
                "plan_date"):
        data[key] = _iso(data.get(key))
    return data


def save_computed_rating(well_id: str, values: dict, *, measure: str | None, provenance: dict) -> dict:
    """Store a well's computed rating, keeping the operator fields.

    measure_since (the start of the measure deadline) moves only when the measure changes, so
    the daily recomputation does not keep pushing deadlines forward.
    """
    now = datetime.now(timezone.utc)
    computed = {key: values.get(key) for key in COMPUTED_RATING_FIELDS}
    computed.update(measure=measure, provenance=provenance, computed_at=now, assessed_at=now, updated_at=now,
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


def rating_scores() -> list[float | None]:
    """The НТЦ score of every well: enough to rank one well without loading all rows."""
    with engine().connect() as conn:
        return [row[0] for row in conn.execute(select(well_rating.c.model_ntc)).all()]


def update_rating_row(well_id: str, values: dict) -> bool:
    """Change operator fields only (plan, Промайн document status, comment)."""
    values = {key: value for key, value in values.items() if key in OPERATOR_RATING_FIELDS}
    with engine().begin() as conn:
        result = conn.execute(well_rating.update().where(well_rating.c.well_id == well_id).values(
            **values, updated_at=datetime.now(timezone.utc)))
        return result.rowcount == 1


def save_ai_proposal(well_id: str, proposal: dict) -> bool:
    with engine().begin() as conn:
        result = conn.execute(well_rating.update().where(well_rating.c.well_id == well_id).values(
            ai_proposal=proposal, ai_proposal_at=datetime.now(timezone.utc)))
        return result.rowcount == 1


# --------------------------------------------------------------------------- documents
DOCUMENT_LIST_COLUMNS = [column for column in documents.c if column.name != "text"]
# The list leaves out the extracted records too: a registry of many wells makes them large.
DOCUMENT_SUMMARY_COLUMNS = [column for column in DOCUMENT_LIST_COLUMNS if column.name != "extraction"]


def _document_row(row, with_text: bool = False) -> dict:
    data = dict(row)
    for key in ("created_at", "started_at", "processed_at", "doc_date"):
        data[key] = _iso(data.get(key))
    if not with_text:
        data.pop("text", None)
    return data


def create_document(values: dict) -> tuple[dict, bool]:
    """Insert a document row; a file with the same SHA-256 returns the existing row instead.

    Returns (row, created)."""
    with engine().begin() as conn:
        created = bool(_merge(conn, documents, [values], ("sha256",), output="document_id"))
        row = conn.execute(select(*DOCUMENT_LIST_COLUMNS).where(
            documents.c.sha256 == values["sha256"])).mappings().one()
    return _document_row(row), created


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
        query = query.where(text(
            f"EXISTS (SELECT 1 FROM OPENJSON(documents.well_ids) "
            f"WHERE [value] COLLATE {BINARY_COLLATION} = :mai_well_id)").bindparams(mai_well_id=well_id))
    with engine().connect() as conn:
        return [_document_row(row) for row in conn.execute(query).mappings().all()]


def document_status_counts() -> dict[str, int]:
    with engine().connect() as conn:
        rows = conn.execute(select(documents.c.status, func.count()).group_by(documents.c.status)).all()
    return {status: count for status, count in rows}


def update_document(document_id: str, **values) -> None:
    with engine().begin() as conn:
        conn.execute(update(documents).where(documents.c.document_id == document_id).values(**values))


def claim_next_document(document_id: str | None = None) -> dict | None:
    """Mark the oldest queued document (or the given one) as processing and return it; safe
    across workers. A document returned to the queue after a failed attempt waits
    MAI_DOCS_RETRY_MINUTES before the next automatic attempt."""
    now = datetime.now(timezone.utc)
    if document_id is not None:
        condition, params = "document_id = :document_id", {"document_id": document_id}
    else:
        condition = "(started_at IS NULL OR started_at < :retry_before)"
        params = {"retry_before": now - timedelta(minutes=settings.docs_retry_minutes)}
    # READPAST skips a row another worker holds; UPDLOCK keeps the chosen one for this update.
    statement = text(
        f"WITH next AS (SELECT TOP (1) document_id, status, started_at, attempts "
        f"FROM {qualified('documents')} WITH (UPDLOCK, READPAST, ROWLOCK) "
        f"WHERE status = 'queued' AND {condition} ORDER BY created_at) "
        f"UPDATE next SET status = 'processing', started_at = :now, attempts = attempts + 1 "
        f"OUTPUT inserted.document_id;"
    ).bindparams(bindparam("now", type_=UTCDateTime()), *(
        [bindparam("retry_before", type_=UTCDateTime())] if "retry_before" in params else []))
    with engine().begin() as conn:
        row = conn.execute(statement, {"now": now, **params}).first()
    return get_document(row[0]) if row else None


def requeue_stale_documents(older_than_minutes: int = 30) -> int:
    """Documents left in processing by a stopped worker go back to the queue."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    with engine().begin() as conn:
        return conn.execute(update(documents).where(documents.c.status == "processing", documents.c.started_at < cutoff)
                            .values(status="queued")).rowcount or 0


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
        conn.execute(delete(source_documents).where(
            func.json_value(source_documents.c.payload_json, "$.document_id") == document_id))


def delete_document_row(document_id: str) -> None:
    with engine().begin() as conn:
        conn.execute(delete(documents).where(documents.c.document_id == document_id))
