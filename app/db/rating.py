"""Daily cycle jobs and the stored well rating (computed fields, operator fields, AI proposals)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.base import _iso, _retry_deadlocks, engine
from app.db.schema import monitor_jobs, well_rating


# --------------------------------------------------------------------------- daily cycle jobs
@_retry_deadlocks
def create_monitor_job(job_id: str) -> dict:
    with engine().begin() as conn:
        conn.execute(monitor_jobs.insert().values(job_id=job_id, status="queued", created_at=datetime.now(timezone.utc)))
    return get_monitor_job(job_id)


@_retry_deadlocks
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
    "model_ntc", "model_ntc_raw", "model_math", "protection", "installed_at", "liquid_rate_m3_day", "oil_pct", "data_gaps",
)
# Edited by the operator; a recomputation never touches them.
OPERATOR_RATING_FIELDS = ("plan_date", "promine_status", "comment")


def _rating_row(row) -> dict:
    data = dict(row)
    for key in ("assessed_at", "updated_at", "measure_since", "computed_at", "installed_at", "ai_proposal_at",
                "plan_date"):
        data[key] = _iso(data.get(key))
    return data


@_retry_deadlocks
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


def rating_scores() -> list[tuple[float | None, float | None]]:
    """The НТЦ and math forecasts of every well: enough to rank one well without loading all rows."""
    with engine().connect() as conn:
        return [(row[0], row[1]) for row in conn.execute(select(well_rating.c.model_ntc, well_rating.c.model_math)).all()]


@_retry_deadlocks
def update_rating_row(well_id: str, values: dict) -> bool:
    """Change operator fields only (plan, Промайн document status, comment)."""
    values = {key: value for key, value in values.items() if key in OPERATOR_RATING_FIELDS}
    with engine().begin() as conn:
        result = conn.execute(well_rating.update().where(well_rating.c.well_id == well_id).values(
            **values, updated_at=datetime.now(timezone.utc)))
        return result.rowcount == 1


@_retry_deadlocks
def save_ai_proposal(well_id: str, proposal: dict) -> bool:
    with engine().begin() as conn:
        result = conn.execute(well_rating.update().where(well_rating.c.well_id == well_id).values(
            ai_proposal=proposal, ai_proposal_at=datetime.now(timezone.utc)))
        return result.rowcount == 1
