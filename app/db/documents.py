"""Documents received as files: their rows, the processing queue and the data they loaded."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import bindparam, delete, func, select, text, update

from app.config import settings
from app.db.base import _iso, _merge, _retry_deadlocks, engine, qualified
from app.db.schema import (BINARY_COLLATION, documents, history_batches, ingestion_batches, source_documents,
                           UTCDateTime, telemetry, history_events, document_contributions)


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
def update_document(document_id: str, *, claim_id: str | None = None, **values) -> bool:
    with engine().begin() as conn:
        condition = documents.c.document_id == document_id
        if claim_id is not None:
            condition &= documents.c.claim_id == claim_id
        return bool(conn.execute(update(documents).where(condition).values(**values)).rowcount)


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
        f"WITH next AS (SELECT TOP (1) document_id, status, started_at, attempts, claim_id "
        f"FROM {qualified('documents')} WITH (UPDLOCK, READPAST, ROWLOCK) "
        f"WHERE status = 'queued' AND {condition} ORDER BY created_at) "
        f"UPDATE next SET status = 'processing', started_at = :now, attempts = attempts + 1, claim_id = :claim_id "
        f"OUTPUT inserted.document_id;"
    ).bindparams(bindparam("now", type_=UTCDateTime()), *(
        [bindparam("retry_before", type_=UTCDateTime())] if "retry_before" in params else []))
    with engine().begin() as conn:
        row = conn.execute(statement, {"now": now, "claim_id": str(uuid.uuid4()), **params}).first()
    return get_document(row[0]) if row else None


@_retry_deadlocks
def requeue_stale_documents(older_than_minutes: int = 30) -> int:
    """Documents left in processing by a stopped worker go back to the queue."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    with engine().begin() as conn:
        return conn.execute(update(documents).where(documents.c.status == "processing", documents.c.started_at < cutoff)
                            .values(status="queued", claim_id=None)).rowcount or 0


@_retry_deadlocks
def reset_document(document_id: str) -> dict | None:
    with engine().begin() as conn:
        row = conn.execute(select(documents.c.status).where(documents.c.document_id == document_id)
                           .with_hint(documents, "WITH (UPDLOCK, ROWLOCK)", "mssql")).first()
        if row is None:
            return None
        if row.status == "processing":
            raise ValueError("Документ уже обрабатывается.")
        conn.execute(update(documents).where(documents.c.document_id == document_id).values(
            status="queued", attempts=0, started_at=None, claim_id=None,
            status_message="Отправлен на повторную обработку."))
    return get_document(document_id)


@_retry_deadlocks
def replace_document_data(document: dict, contributions: list[dict], **values) -> list[str]:
    """Commit one accepted version and its metadata together, fenced by the processing claim."""
    document_id = document["document_id"]
    with engine().begin() as conn:
        current = conn.execute(select(documents).where(documents.c.document_id == document_id)
                               .with_hint(documents, "WITH (UPDLOCK, ROWLOCK)", "mssql")).mappings().one()
        if current["claim_id"] != document.get("claim_id") or current["status"] != "processing":
            raise ValueError("Задание документа уже передано другому обработчику.")
        old_wells = (current["loaded"] or {}).get("wells") or []
        conn.execute(delete(document_contributions).where(document_contributions.c.document_id == document_id))
        if contributions:
            conn.execute(document_contributions.insert(), [{**row, "document_id": document_id,
                "updated_at": datetime.now(timezone.utc)} for row in contributions])
        conn.execute(update(documents).where(documents.c.document_id == document_id).values(**values))
    return sorted(set(old_wells) | {row["well_id"] for row in contributions})


def contribution_rows(well_id: str) -> list[dict]:
    with engine().connect() as conn:
        return [dict(row) for row in conn.execute(select(document_contributions)
            .where(document_contributions.c.well_id == well_id)
            .order_by(document_contributions.c.updated_at, document_contributions.c.document_id)).mappings()]


@_retry_deadlocks
def delete_document_row(document_id: str) -> dict | None:
    with engine().begin() as conn:
        row = conn.execute(select(documents).where(documents.c.document_id == document_id)
                           .with_hint(documents, "WITH (UPDLOCK, ROWLOCK)", "mssql")).mappings().first()
        if row is None:
            return None
        if row["status"] == "processing":
            raise ValueError("Документ уже обрабатывается.")
        conn.execute(delete(documents).where(documents.c.document_id == document_id))
    return _document_row(row)


def migrate_document_data(conn) -> None:
    """Move proven legacy ownership; keep ambiguous external/profile batches conservatively."""
    for document in conn.execute(select(documents)).mappings().all():
        loaded = document["loaded"] or {}
        if not loaded or loaded.get("storage_version") == 2:
            continue
        document_id = document["document_id"]
        prefix = f"doc-{document_id}-%"
        groups = {}
        def group(well, source):
            return groups.setdefault((well, source), {"records": [], "work_history": [], "failure_history": []})
        # Explicit legacy profile batch ids may also belong to independent imports: copy, do not delete.
        extra = loaded.get("batch_ids") or []
        batches = [prefix] + [extra[i:i+1000] for i in range(0, len(extra), 1000)]
        for selection in batches:
            for table, batch_table in ((telemetry, ingestion_batches), (history_events, history_batches)):
                condition = table.c.batch_id.like(selection) if isinstance(selection, str) else table.c.batch_id.in_(selection)
                for row in conn.execute(select(table).where(condition)).mappings():
                    data = group(row["well_id"], row["source_id"])
                    key = "records" if table is telemetry else "work_history" if row["event_type"] == "work" else "failure_history"
                    data[key].append(row["payload_json"])
        for row in conn.execute(select(source_documents).where(
                func.json_value(source_documents.c.payload_json, "$.document_id") == document_id)).mappings():
            group(row["well_id"], row["source_id"])["payload"] = row["payload_json"]
        for (well, source), data in groups.items():
            _merge(conn, document_contributions, [{"document_id": document_id, "well_id": well,
                "source_id": source, "data": data, "updated_at": document["processed_at"] or document["created_at"]}],
                ("document_id", "well_id", "source_id"))
        conn.execute(delete(ingestion_batches).where(ingestion_batches.c.batch_id.like(prefix)))
        conn.execute(delete(history_batches).where(history_batches.c.batch_id.like(prefix)))
        conn.execute(delete(source_documents).where(
            func.json_value(source_documents.c.payload_json, "$.document_id") == document_id))
        conn.execute(update(documents).where(documents.c.document_id == document_id).values(
            loaded={**loaded, "storage_version": 2, "legacy_external_batches_preserved": bool(extra)}))
