"""Documents received as files: their rows, the processing queue and the data they loaded."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import bindparam, delete, func, select, text, update

from app.config import settings
from app.db.base import _iso, _merge, _retry_deadlocks, engine, qualified
from app.db.schema import (BINARY_COLLATION, documents, history_batches, ingestion_batches, source_documents,
                           UTCDateTime)


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


@_retry_deadlocks
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


@_retry_deadlocks
def update_document(document_id: str, **values) -> None:
    with engine().begin() as conn:
        conn.execute(update(documents).where(documents.c.document_id == document_id).values(**values))


@_retry_deadlocks
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


@_retry_deadlocks
def requeue_stale_documents(older_than_minutes: int = 30) -> int:
    """Documents left in processing by a stopped worker go back to the queue."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    with engine().begin() as conn:
        return conn.execute(update(documents).where(documents.c.status == "processing", documents.c.started_at < cutoff)
                            .values(status="queued")).rowcount or 0


@_retry_deadlocks
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


@_retry_deadlocks
def delete_document_row(document_id: str) -> None:
    with engine().begin() as conn:
        conn.execute(delete(documents).where(documents.c.document_id == document_id))
