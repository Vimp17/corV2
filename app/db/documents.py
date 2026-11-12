"""Documents received as files: their rows, the processing queue and the data they loaded."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import json
import uuid

from sqlalchemy import UnicodeText, bindparam, case, cast, delete, func, literal_column, or_, select, text, update

from app.config import settings
from app.db.base import _iso, _merge, _retry_deadlocks, engine, qualified
from app.db.recompute import enqueue
from app.db.schema import (BINARY_COLLATION, documents, history_batches, ingestion_batches, source_documents,
                           UTCDateTime, telemetry, history_events, document_contributions)


# --------------------------------------------------------------------------- documents
def _accepted_data():
    loaded = case((func.isjson(documents.c.loaded) == 1, documents.c.loaded), else_=literal_column("N'{}'"))
    return or_(func.json_value(loaded, "$.wells[0]").is_not(None),
               select(document_contributions.c.document_id).where(
                   document_contributions.c.document_id == documents.c.document_id).exists())


ACCEPTED_COLUMN = case((_accepted_data(), True), else_=False).label("has_accepted_data")
DOCUMENT_LIST_COLUMNS = [column for column in documents.c if column.name != "text"] + [ACCEPTED_COLUMN]
# The list leaves out the extracted records too: a registry of many wells makes them large.
DOCUMENT_SUMMARY_COLUMNS = [column for column in DOCUMENT_LIST_COLUMNS if column.name != "extraction"]


def _document_row(row, with_text: bool = False) -> dict:
    data = dict(row)
    data.setdefault("has_accepted_data", bool((data.get("loaded") or {}).get("wells")))
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
    columns = [*documents.c, ACCEPTED_COLUMN] if with_text else DOCUMENT_LIST_COLUMNS
    with engine().connect() as conn:
        row = conn.execute(select(*columns).where(documents.c.document_id == document_id)).mappings().first()
    return _document_row(row, with_text) if row else None


def _document_filters(well_id=None, status=None, q=None):
    conditions = []
    if status:
        conditions.append(documents.c.status == status)
    if well_id:
        conditions.append(text(
            f"EXISTS (SELECT 1 FROM OPENJSON(CASE WHEN ISJSON(documents.well_ids)=1 "
            f"THEN documents.well_ids ELSE N'[]' END) "
            f"WHERE [value] COLLATE {BINARY_COLLATION} = :mai_well_id)").bindparams(mai_well_id=well_id))
    if q:
        conditions.append(or_(documents.c.document_id == q,
                              documents.c.filename.collate("Latin1_General_100_CI_AS").contains(q, autoescape=True),
                              cast(documents.c.well_ids, UnicodeText).collate("Latin1_General_100_CI_AS")
                              .contains(q, autoescape=True)))
    return conditions


def list_documents(*, well_id: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
    query = select(*DOCUMENT_SUMMARY_COLUMNS).where(*_document_filters(well_id, status)).order_by(
        documents.c.created_at.desc(), documents.c.document_id.desc()).limit(limit)
    with engine().connect() as conn:
        return [_document_row(row) for row in conn.execute(query).mappings().all()]


def document_page(*, well_id=None, status=None, q=None, limit=200, cursor=None) -> dict:
    conditions = _document_filters(well_id, status, q)
    query = select(*DOCUMENT_SUMMARY_COLUMNS).where(*conditions)
    if cursor:
        try:
            when, identity = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            when = datetime.fromisoformat(when)
            if not isinstance(identity, str) or len(identity) > 36:
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Неверная страница истории. Обновите список.") from exc
        query = query.where(or_(documents.c.created_at < when,
                                (documents.c.created_at == when) & (documents.c.document_id < identity)))
    with engine().connect() as conn:
        rows = conn.execute(query.order_by(documents.c.created_at.desc(), documents.c.document_id.desc())
                            .limit(limit + 1)).mappings().all()
        total = conn.scalar(select(func.count()).select_from(documents).where(*conditions))
        accepted = conn.scalar(select(func.count()).select_from(documents).where(
            documents.c.status.not_in(("queued", "processing")), _accepted_data()))
        cleanup = conn.scalar(select(func.count()).select_from(documents).where(
            documents.c.status.in_(("failed", "not_recognized")), ~_accepted_data()))
    next_cursor = base64.urlsafe_b64encode(json.dumps(
        [_iso(rows[limit-1]["created_at"]), rows[limit-1]["document_id"]]).encode()).decode() if len(rows) > limit else None
    return {"documents": [_document_row(row) for row in rows[:limit]], "matching_count": total,
            "next_cursor": next_cursor, "accepted_count": accepted, "safe_cleanup_count": cleanup}


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
        wells = sorted(set(old_wells) | {row["well_id"] for row in contributions})
        enqueue(conn, wells)
    return wells


def contribution_rows(well_id: str) -> list[dict]:
    with engine().connect() as conn:
        return [dict(row) for row in conn.execute(select(document_contributions)
            .where(document_contributions.c.well_id == well_id)
            .order_by(document_contributions.c.updated_at, document_contributions.c.document_id)).mappings()]


@_retry_deadlocks
def delete_document_row(document_id: str, *, only_without_data: bool = False) -> dict | None:
    with engine().begin() as conn:
        row = conn.execute(select(documents, ACCEPTED_COLUMN).where(documents.c.document_id == document_id)
                           .with_hint(documents, "WITH (UPDLOCK, ROWLOCK)", "mssql")).mappings().first()
        if row is None:
            return None
        if row["status"] == "processing":
            raise ValueError("Документ уже обрабатывается.")
        if only_without_data and row["has_accepted_data"]:
            return None
        enqueue(conn, (row["loaded"] or {}).get("wells") or [])
        conn.execute(delete(documents).where(documents.c.document_id == document_id))
    return _document_row(row)


def migrate_document_data(conn) -> None:
    """Move proven legacy ownership; keep ambiguous external/profile batches conservatively."""
    loaded_json = case((func.isjson(documents.c.loaded) == 1, documents.c.loaded), else_=literal_column("N'{}'"))
    query = select(documents.c.document_id, documents.c.loaded, documents.c.processed_at, documents.c.created_at).where(
        func.isjson(documents.c.loaded) == 1,
        func.coalesce(func.json_value(loaded_json, "$.storage_version"), "") != "2")
    for document in conn.execute(query).mappings().all():
        loaded = document["loaded"] or {}
        if not loaded:
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
            for table in (telemetry, history_events):
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
        enqueue(conn, loaded.get("wells") or [])
