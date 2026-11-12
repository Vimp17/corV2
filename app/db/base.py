"""Connection, the start-up check of the tables, application locks, deadlocks and MERGE."""
from __future__ import annotations

import functools
import logging
import random
import re
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateColumn
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import ClauseElement

from app.config import settings
from app.db.schema import metadata, OBSOLETE_COLUMNS, OBSOLETE_TABLES

logger = logging.getLogger(__name__)

# SQL Server accepts at most 2100 parameters in one request and 1000 rows in one VALUES list.
MAX_PARAMETERS = 2000
MAX_VALUES_ROWS = 1000
# Wait for the schema lock of another starting process (milliseconds).
SCHEMA_LOCK_TIMEOUT_MS = 600_000


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


def _applock(conn, resource: str, timeout_ms: int, owner: str = "Session") -> bool:
    """Owner "Session": until _release; "Transaction": until the commit or rollback."""
    # NOCOUNT only around the call: rowcount of later statements on this connection must stay.
    result = conn.execute(text(
        "SET NOCOUNT ON; DECLARE @result int; "
        "EXEC @result = sp_getapplock @Resource = :resource, @LockMode = 'Exclusive', "
        f"@LockOwner = '{'Transaction' if owner == 'Transaction' else 'Session'}', @LockTimeout = :timeout; "
        "SET NOCOUNT OFF; SELECT @result"), {"resource": resource, "timeout": timeout_ms}).scalar()
    return result is not None and result >= 0


def _release(conn, resource: str) -> None:
    conn.execute(text("EXEC sp_releaseapplock @Resource = :resource, @LockOwner = 'Session'"), {"resource": resource})


def _lock_name(key: str) -> str:
    return f"mai:{settings.database_schema or 'default'}:{key}"


def schema_changes(conn) -> dict[str, list[str]]:
    """What the database lacks or holds in excess against the tables above: tables to create, columns
    to add ("table.column"), tables and columns of earlier versions to drop."""
    schema = settings.database_schema or None
    inspector = inspect(conn)
    present = {name.lower() for name in inspector.get_table_names(schema=schema)}
    changes: dict[str, list[str]] = {"create": [], "add": [], "drop": []}
    for table in metadata.sorted_tables:
        if table.name not in present:
            changes["create"].append(table.name)
            continue
        existing = {column["name"].lower() for column in inspector.get_columns(table.name, schema=schema)}
        changes["add"] += [f"{table.name}.{column.name}" for column in table.columns
                           if column.name.lower() not in existing]
        changes["drop"] += [f"{table.name}.{name}" for name in OBSOLETE_COLUMNS.get(table.name, ()) if name in existing]
    changes["drop"] += [name for name in OBSOLETE_TABLES if name in present]
    return changes


_CHANGE_WORDS = {"create": ("созданы таблицы", "создать таблицы"), "add": ("добавлены столбцы", "добавить столбцы"),
                 "drop": ("удалены таблицы и столбцы прежних версий", "удалить таблицы и столбцы прежних версий")}


def describe_changes(changes: dict[str, list[str]], done: bool = True) -> str:
    return "; ".join(f"{_CHANGE_WORDS[key][0 if done else 1]}: {', '.join(names)}"
                     for key, names in changes.items() if names)


def _server_message(exc: DBAPIError) -> str:
    """The server's words from a driver error, without pymssql's DB-Lib trailer or pyodbc's [..] prefixes."""
    args = getattr(exc.orig, "args", ())
    message = args[1] if len(args) > 1 else str(exc.orig or exc)
    if isinstance(message, bytes):
        message = message.decode("utf-8", "replace")
    message = re.sub(r"^(\[[^\]]*\]\s*)+", "", str(message)).split("DB-Lib error message")[0]
    return " ".join(message.split())[:300] or str(exc)[:300]


def _apply_changes(conn, changes: dict[str, list[str]]) -> None:
    quote = conn.dialect.identifier_preparer.quote
    if changes["create"]:
        metadata.create_all(conn, tables=[metadata.tables[name] for name in changes["create"]])
    for item in changes["add"]:
        table, column = item.split(".", 1)
        ddl = CreateColumn(metadata.tables[table].c[column]).compile(dialect=conn.dialect)
        conn.execute(text(f"ALTER TABLE {qualified(table)} ADD {ddl}"))
    for item in changes["drop"]:
        table, _, column = item.partition(".")
        conn.execute(text(f"ALTER TABLE {qualified(table)} DROP COLUMN {quote(column)}" if column
                          else f"DROP TABLE {qualified(table)}"))


def init_db() -> dict[str, list[str]]:
    """Check the tables at start: create what is missing, drop what earlier versions left, check again.
    Processes starting at once wait for one another (an application lock). Returns what was changed."""
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
            changes = schema_changes(conn)
            if not any(changes.values()):
                from app.db.documents import migrate_document_data
                migrate_document_data(conn)
                conn.commit()
                logger.info("Таблицы базы данных на месте: %d из %d.", len(metadata.tables), len(metadata.tables))
                return changes
            try:
                _apply_changes(conn, changes)
                conn.commit()
            except DBAPIError as exc:
                conn.rollback()
                raise RuntimeError(f"Не удалось {describe_changes(changes, done=False)} — {_server_message(exc)} "
                                   "Нужны права CREATE TABLE и ALTER на схему; без них таблицы один раз создаёт "
                                   "администратор.") from exc
            left = schema_changes(conn)
            if left["create"] or left["add"]:
                raise RuntimeError("После создания в базе нет таблиц и столбцов: "
                                   + ", ".join(left["create"] + left["add"]))
            from app.db.documents import migrate_document_data
            migrate_document_data(conn)
            conn.commit()
            logger.info("Таблицы базы данных: %s.", describe_changes(changes))
            return changes
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


# --------------------------------------------------------------------------- deadlocks
DEADLOCK_ATTEMPTS = 4
WRITE_QUEUE_TIMEOUT_MS = 60_000


def _queue_writes(conn, table: str) -> None:
    """Writers of a table wait for each other until the commit: MERGE … HOLDLOCK of wells written at the
    same time locks the same key ranges and deadlocks, a wait of a few milliseconds is cheaper."""
    if not _applock(conn, _lock_name(f"write:{table}"), WRITE_QUEUE_TIMEOUT_MS, owner="Transaction"):
        raise TimeoutError(f"Запись в {table} ждёт другую запись дольше {WRITE_QUEUE_TIMEOUT_MS // 1000} с.")


def _deadlocked(exc: DBAPIError) -> bool:
    """SQL Server chose the transaction as a deadlock victim (error 1205; SQLSTATE 40001 through ODBC)."""
    args = getattr(exc.orig, "args", None) or ()
    return bool(args) and (args[0] in (1205, "40001") or "deadlock" in str(exc.orig).lower())


def _retry_deadlocks(function):
    """Concurrent writes (parallel pushes, the documents worker, the daily cycle) may deadlock: the victim's
    transaction is rolled back whole, so the write is run again after a short random pause."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        for attempt in range(1, DEADLOCK_ATTEMPTS + 1):
            try:
                return function(*args, **kwargs)
            except DBAPIError as exc:
                if attempt == DEADLOCK_ATTEMPTS or not _deadlocked(exc):
                    raise
                logger.warning("Deadlock in %s, attempt %d of %d", function.__name__, attempt, DEADLOCK_ATTEMPTS)
                time.sleep(random.uniform(0.05, 0.25) * attempt)
    return wrapper


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


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
