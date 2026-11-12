"""Durable, revision-fenced rating invalidations committed with document changes."""
from datetime import datetime, timezone
import uuid

from sqlalchemy import delete, select, update

from app.db.base import _merge, _queue_writes, _retry_deadlocks, engine
from app.db.schema import rating_recompute_queue


def enqueue(conn, wells) -> None:
    rows = [{"well_id": well, "revision": str(uuid.uuid4()), "queued_at": datetime.now(timezone.utc),
             "last_error": None} for well in sorted(set(wells))]
    if rows:
        _queue_writes(conn, "rating_recompute_queue")
        _merge(conn, rating_recompute_queue, rows, ("well_id",), update=("revision", "queued_at", "last_error"))


def pending(well_ids=None, limit=200) -> list[dict]:
    ids = list(dict.fromkeys(well_ids)) if well_ids is not None else None
    batches = [ids[i:i+1000] for i in range(0, len(ids), 1000)] if ids is not None else [None]
    rows = []
    with engine().connect() as conn:
        for batch in batches:
            query = select(rating_recompute_queue).order_by(
                rating_recompute_queue.c.queued_at, rating_recompute_queue.c.well_id)
            if batch is not None:
                query = query.where(rating_recompute_queue.c.well_id.in_(batch))
            rows.extend(dict(row) for row in conn.execute(query.limit(limit-len(rows))).mappings())
            if len(rows) >= limit:
                break
    return rows


@_retry_deadlocks
def finish(job, error=None) -> None:
    table = rating_recompute_queue
    condition = (table.c.well_id == job["well_id"]) & (table.c.revision == job["revision"])
    with engine().begin() as conn:
        statement = (update(table).values(last_error=str(error)[:2000], queued_at=datetime.now(timezone.utc))
                     if error is not None else delete(table))
        conn.execute(statement.where(condition))
