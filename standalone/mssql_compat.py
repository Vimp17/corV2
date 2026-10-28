"""Microsoft SQL Server instead of PostgreSQL — the MSSQL mode of the standalone build.

MAI_STANDALONE_DB=mssql keeps the tables in a SQL Server database (2016 or later, database
compatibility level 130 or higher for the JSON functions). The connection goes through pyodbc
and the "ODBC Driver 17/18 for SQL Server" installed on the computer: MAI_DATABASE_URL is the
mssql+pyodbc URL written by "mai.cmd setup", MAI_STANDALONE_MSSQL_SCHEMA the schema of the
tables (empty: the default schema of the login, usually dbo).

app/db.py is written for PostgreSQL. install() adapts that same module in place, before the
application uses it, so app/ itself is unchanged:
  - column types: text is NVARCHAR with a binary collation, so keys compare exactly as in
    PostgreSQL whatever the collation of the database (DEMO-1 and demo-1 stay two wells);
    longer than 4000 characters → NVARCHAR(MAX); JSON → NVARCHAR(MAX); time-zone-aware
    timestamps → DATETIME2 in UTC, read back aware;
  - the engine: every statement in the configured schema (schema_translate_map);
  - INSERT … ON CONFLICT (the PostgreSQL insert of app/db.py) → MERGE … WITH (HOLDLOCK), so the
    insert-or-update and insert-if-new logic of app/db.py runs unchanged;
  - DISTINCT ON → ROW_NUMBER(); JSONB → JSON_VALUE / JSON_QUERY / OPENJSON; advisory locks →
    sp_getapplock; FOR UPDATE SKIP LOCKED → UPDLOCK, READPAST; the plain-SQL queries → the same
    queries built by SQLAlchemy, so they find the tables in the configured schema;
  - schema: created from the table definitions (create_all) instead of the Alembic migrations
    (written for PostgreSQL); columns added by a later version are added to the existing
    tables (ALTER TABLE … ADD).
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

from sqlalchemy import (DateTime, String, Text, Unicode, UnicodeText, bindparam, create_engine, delete, func,
                        inspect, literal_column, select, text, union, update)
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateColumn
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.types import TypeDecorator

# Exact comparison and ordering of text, independent of the collation of the database.
BINARY_COLLATION = "Latin1_General_100_BIN2"
# SQL Server accepts at most 2100 parameters in one request and 1000 rows in one VALUES list.
MAX_PARAMETERS = 2000
MAX_VALUES_ROWS = 1000
# Wait for the schema lock of another starting process (milliseconds).
SCHEMA_LOCK_TIMEOUT_MS = 600_000

_installed = False
_schema = ""


class UTCDateTime(TypeDecorator):
    """A moment stored as UTC in DATETIME2 and returned with tzinfo=UTC (naive = UTC, as in app)."""

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


def _text_type(kind: String):
    length = getattr(kind, "length", None)
    if isinstance(kind, Text) or length is None or length > 4000:
        return UnicodeText(collation=BINARY_COLLATION)
    return Unicode(length, collation=BINARY_COLLATION)


# --------------------------------------------------------------------------- INSERT … ON CONFLICT → MERGE
class _Source:
    """statement.excluded.<column>: the value proposed for insertion (the MERGE source row)."""

    def __init__(self, name: str) -> None:
        self.name = name


class _Excluded:
    def __getattr__(self, name: str) -> _Source:
        return _Source(name)


class Upsert(Executable, ClauseElement):
    """The subset of the PostgreSQL insert used by app/db.py, compiled to a SQL Server MERGE.

    pg_insert(table).values(rows | **row).on_conflict_do_nothing(index_elements=[...]) or
    .on_conflict_do_update(index_elements=[...], set_={...}), optionally .returning(column):
    rows whose key exists are skipped or updated, the others inserted; RETURNING lists the
    inserted rows only, as ON CONFLICT DO NOTHING … RETURNING does.
    """

    inherit_cache = False
    _execution_options = Executable._execution_options

    def __init__(self, table) -> None:
        self.table = table
        self.rows: list[dict] = []
        self.keys: list[str] = []
        self.set_: dict[str, Any] | None = None
        self.output: str | None = None
        self.excluded = _Excluded()

    def values(self, rows: Iterable[dict] | None = None, **row) -> "Upsert":
        self.rows = [dict(item) for item in rows] if rows is not None else [row]
        return self

    def _conflict(self, index_elements) -> list[str]:
        return [getattr(column, "name", column) for column in index_elements]

    def on_conflict_do_nothing(self, index_elements) -> "Upsert":
        self.keys, self.set_ = self._conflict(index_elements), None
        return self

    def on_conflict_do_update(self, index_elements, set_: dict) -> "Upsert":
        self.keys, self.set_ = self._conflict(index_elements), dict(set_)
        return self

    def returning(self, column) -> "Upsert":
        self.output = column.name
        return self


@compiles(Upsert, "mssql")
def _compile_upsert(element: Upsert, compiler, **kw) -> str:
    table, quote = element.table, compiler.preparer.quote
    # Rows proposing the same key: the first one wins, as when PostgreSQL skips the later ones.
    rows, seen = [], set()
    for row in element.rows:
        key = tuple(row.get(name) for name in element.keys)
        if key not in seen:
            seen.add(key)
            rows.append(row)
    columns = list(rows[0])
    values = []
    for index, row in enumerate(rows):
        cells = [compiler.process(bindparam(f"u{index}_{position}", row.get(name), type_=table.c[name].type,
                                            unique=True), **kw)
                 for position, name in enumerate(columns)]
        values.append("(" + ", ".join(cells) + ")")
    source = ", ".join(quote(name) for name in columns)
    on = " AND ".join(f"t.{quote(name)} = s.{quote(name)}" for name in element.keys)
    sql = (f"MERGE {compiler.preparer.format_table(table)} WITH (HOLDLOCK) AS t "
           f"USING (VALUES {', '.join(values)}) AS s ({source}) ON {on}")
    if element.set_:
        assignments = []
        for name, value in element.set_.items():
            target = value.name if isinstance(value, _Source) else None
            rendered = (f"s.{quote(target)}" if target else
                        compiler.process(bindparam(f"set_{name}", value, type_=table.c[name].type, unique=True), **kw))
            assignments.append(f"t.{quote(name)} = {rendered}")
        sql += " WHEN MATCHED THEN UPDATE SET " + ", ".join(assignments)
    sql += (f" WHEN NOT MATCHED THEN INSERT ({source}) VALUES ("
            + ", ".join(f"s.{quote(name)}" for name in columns) + ")")
    if element.output:
        sql += f" OUTPUT inserted.{quote(element.output)}"
    return sql + ";"


def upsert(table) -> Upsert:
    return Upsert(table)


# --------------------------------------------------------------------------- install
def qualified(name: str, dialect) -> str:
    """A table name for hand-written SQL, in the configured schema."""
    quote = dialect.identifier_preparer.quote
    return (quote(_schema) + "." if _schema else "") + quote(name)


def install(url: str, schema: str = "") -> None:
    """Point app.db at SQL Server and replace its PostgreSQL-only parts."""
    global _installed, _schema
    if _installed:
        return
    from app import db

    _schema = (schema or "").strip()
    for table in db.metadata.tables.values():
        for column in table.columns:
            kind = column.type
            if isinstance(kind, DateTime) and getattr(kind, "timezone", False):
                column.type = UTCDateTime()
            elif isinstance(kind, String):
                column.type = _text_type(kind)

    engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10,
                           connect_args={"timeout": 30})
    if _schema:
        engine = engine.execution_options(schema_translate_map={None: _schema})
    db._engine = engine
    db.pg_insert = upsert
    dialect = engine.dialect
    lock_prefix = f"mai:{_schema or 'default'}:"

    def _chunks(rows: list[dict], size: int | None = None):
        width = max(1, len(rows[0])) if rows else 1
        size = max(1, min(MAX_VALUES_ROWS, MAX_PARAMETERS // width))
        for start in range(0, len(rows), size):
            yield rows[start:start + size]

    def _applock(conn, resource: str, timeout_ms: int) -> bool:
        # NOCOUNT only around the call: rowcount of later statements on this connection must stay.
        result = conn.execute(text(
            "SET NOCOUNT ON; DECLARE @result int; "
            "EXEC @result = sp_getapplock @Resource = :resource, @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = :timeout; "
            "SET NOCOUNT OFF; SELECT @result"), {"resource": resource, "timeout": timeout_ms}).scalar()
        return result is not None and result >= 0

    def _release(conn, resource: str) -> None:
        conn.execute(text("EXEC sp_releaseapplock @Resource = :resource, @LockOwner = 'Session'"),
                      {"resource": resource})

    def init_db() -> None:
        """Create the schema and the tables from their definitions; add columns new versions define."""
        with engine.connect() as conn:
            if _schema:
                conn.execute(text("DECLARE @sql nvarchar(400) = N'CREATE SCHEMA ' + QUOTENAME(:schema); "
                                  "IF SCHEMA_ID(:schema) IS NULL EXEC sp_executesql @sql"), {"schema": _schema})
                conn.commit()
            resource = lock_prefix + "schema"
            if not _applock(conn, resource, SCHEMA_LOCK_TIMEOUT_MS):
                raise RuntimeError("Таблицы создаёт другой процесс; повторите позже.")
            conn.commit()
            try:
                db.metadata.create_all(conn)
                inspector = inspect(conn)
                for table in db.metadata.sorted_tables:
                    existing = {column["name"].lower() for column in
                                inspector.get_columns(table.name, schema=_schema or None)}
                    for column in table.columns:
                        if column.name.lower() not in existing:
                            ddl = CreateColumn(column).compile(dialect=dialect)
                            conn.execute(text(f"ALTER TABLE {qualified(table.name, dialect)} ADD {ddl}"))
                conn.commit()
            finally:
                _release(conn, resource)
                conn.commit()

    @contextmanager
    def advisory_lock(key: int) -> Iterator[bool]:
        """Try to take an application lock of this database; yields whether it was acquired."""
        resource = lock_prefix + str(key)
        with engine.connect() as conn:
            acquired = _applock(conn, resource, 0)
            conn.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    _release(conn, resource)
                    conn.commit()

    def _latest(table, partition, columns=None):
        ranked = select(*(columns or [table]), func.row_number().over(
            partition_by=partition, order_by=table.c.created_at.desc()).label("rn")).subquery()
        return ranked, select(*[column for column in ranked.c if column.name != "rn"]).where(ranked.c.rn == 1)

    def latest_analysis_runs() -> list[dict]:
        runs = db.analysis_runs
        ranked, query = _latest(runs, (runs.c.well_id, runs.c.horizon_days))
        with engine.connect() as conn:
            rows = conn.execute(query.order_by(ranked.c.well_id, ranked.c.horizon_days)).mappings().all()
        return [{
            "analysis_id": row["analysis_id"], "well_id": row["well_id"],
            "horizon_days": row["horizon_days"],
            "created_at": row["created_at"].isoformat(), "status": row["status"],
            "analysis": row["result_json"], "agent_response": row["agent_response_json"],
            "agent_status": row["agent_status"],
            "agent_updated_at": row["agent_updated_at"].isoformat() if row["agent_updated_at"] else None,
        } for row in rows]

    def prune_analysis_runs(retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        runs = db.analysis_runs
        ranked, _ = _latest(runs, (runs.c.well_id, runs.c.horizon_days), [runs.c.analysis_id, runs.c.created_at])
        keep = select(ranked.c.analysis_id).where(ranked.c.rn == 1)
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with engine.begin() as conn:
            return conn.execute(delete(runs).where(runs.c.created_at < cutoff,
                                                   runs.c.analysis_id.not_in(keep))).rowcount or 0

    def latest_era_proposals() -> dict[str, dict]:
        proposals = db.era_proposals
        _, query = _latest(proposals, (proposals.c.well_id,),
                           [proposals.c.well_id, proposals.c.proposal_id, proposals.c.status,
                            proposals.c.change_type, proposals.c.created_at])
        with engine.connect() as conn:
            return {row["well_id"]: db._proposal_row(row) for row in conn.execute(query).mappings().all()}

    def list_wells() -> list[dict]:
        telemetry, history = db.telemetry, db.history_events
        ids = union(select(telemetry.c.well_id), select(history.c.well_id), select(db.analysis_runs.c.well_id),
                    select(db.source_health.c.well_id)).subquery("w")
        history_count = (select(func.count()).select_from(history).where(history.c.well_id == ids.c.well_id)
                         .scalar_subquery())
        query = (select(ids.c.well_id, func.count(telemetry.c.id).label("record_count"),
                        func.min(telemetry.c.timestamp).label("first_timestamp"),
                        func.max(telemetry.c.timestamp).label("last_timestamp"),
                        history_count.label("history_count"))
                 .select_from(ids.outerjoin(telemetry, telemetry.c.well_id == ids.c.well_id))
                 .group_by(ids.c.well_id).order_by(ids.c.well_id))
        with engine.connect() as conn:
            return [dict(row) for row in conn.execute(query).mappings().all()]

    def known_well_ids() -> list[str]:
        query = union(select(db.telemetry.c.well_id), select(db.history_events.c.well_id),
                      select(db.well_rating.c.well_id), select(db.source_health.c.well_id))
        with engine.connect() as conn:
            wells = {row[0] for row in conn.execute(query).all()}
        return sorted(wells | set(db.settings.configured_well_ids))

    def list_documents(*, well_id: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
        documents = db.documents
        query = select(*db.DOCUMENT_SUMMARY_COLUMNS).order_by(documents.c.created_at.desc()).limit(limit)
        if status:
            query = query.where(documents.c.status == status)
        if well_id:
            query = query.where(text(
                f"EXISTS (SELECT 1 FROM OPENJSON(documents.well_ids) "
                f"WHERE [value] COLLATE {BINARY_COLLATION} = :mai_well_id)").bindparams(mai_well_id=well_id))
        with engine.connect() as conn:
            return [db._document_row(row) for row in conn.execute(query).mappings().all()]

    def document_status_counts() -> dict[str, int]:
        documents = db.documents
        with engine.connect() as conn:
            rows = conn.execute(select(documents.c.status, func.count()).group_by(documents.c.status)).all()
        return {status: count for status, count in rows}

    def claim_next_document(document_id: str | None = None) -> dict | None:
        now = datetime.now(timezone.utc)
        if document_id is not None:
            condition, params = "document_id = :document_id", {"document_id": document_id}
        else:
            condition = "(started_at IS NULL OR started_at < :retry_before)"
            params = {"retry_before": now - timedelta(minutes=db.settings.docs_retry_minutes)}
        # READPAST skips a row another worker holds; UPDLOCK keeps the chosen one for this update.
        statement = text(
            f"WITH next AS (SELECT TOP (1) document_id, status, started_at, attempts "
            f"FROM {qualified('documents', dialect)} WITH (UPDLOCK, READPAST, ROWLOCK) "
            f"WHERE status = 'queued' AND {condition} ORDER BY created_at) "
            f"UPDATE next SET status = 'processing', started_at = :now, attempts = attempts + 1 "
            f"OUTPUT inserted.document_id;"
        ).bindparams(bindparam("now", type_=UTCDateTime()), *(
            [bindparam("retry_before", type_=UTCDateTime())] if "retry_before" in params else []))
        with engine.begin() as conn:
            row = conn.execute(statement, {"now": now, **params}).first()
        return db.get_document(row[0]) if row else None

    def requeue_stale_documents(older_than_minutes: int = 30) -> int:
        documents = db.documents
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
        with engine.begin() as conn:
            return conn.execute(update(documents).where(documents.c.status == "processing",
                                                        documents.c.started_at < cutoff)
                                .values(status="queued")).rowcount or 0

    def delete_document_data(document_id: str, extra_batch_ids: Iterable[str] = ()) -> None:
        prefix = f"doc-{document_id}-%"
        extra = [item for item in extra_batch_ids if item]
        with engine.begin() as conn:
            conn.execute(delete(db.ingestion_batches).where(db.ingestion_batches.c.batch_id.like(prefix)))
            conn.execute(delete(db.history_batches).where(db.history_batches.c.batch_id.like(prefix)))
            if extra:
                conn.execute(delete(db.ingestion_batches).where(db.ingestion_batches.c.batch_id.in_(extra)))
                conn.execute(delete(db.history_batches).where(db.history_batches.c.batch_id.in_(extra)))
            conn.execute(delete(db.source_documents).where(
                func.json_value(db.source_documents.c.payload_json, "$.document_id") == document_id))

    def load_extra_measurements(well_id: str) -> list[dict]:
        telemetry = db.telemetry
        extras = func.json_query(telemetry.c.payload_json, "$.extra_measurements")
        query = (select(telemetry.c.source_id, telemetry.c.timestamp, extras.label("extras"))
                 .where(telemetry.c.well_id == well_id, func.left(extras, 1) == literal_column("'{'"))
                 .order_by(telemetry.c.timestamp, telemetry.c.id))
        with engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        return [{"source_id": None if row["source_id"] == db._UNSPECIFIED_SOURCE else row["source_id"],
                 "timestamp": row["timestamp"],
                 "extras": json.loads(row["extras"]) if isinstance(row["extras"], str) else row["extras"]}
                for row in rows]

    for function in (_chunks, init_db, advisory_lock, latest_analysis_runs, prune_analysis_runs,
                     latest_era_proposals, list_wells, known_well_ids, list_documents, document_status_counts,
                     claim_next_document, requeue_stale_documents, delete_document_data, load_extra_measurements):
        setattr(db, function.__name__, function)
    _installed = True


def installed_schema() -> str:
    return _schema
