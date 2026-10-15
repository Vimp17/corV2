"""Well rating: priority index from the НТЦ and math model scores and the measure it implies.

The rules mirror the approved Excel rating (sheet «Параметры»):
  index >= repair_from          -> planned repair package, well goes to the ERA repair plan
  ud_to < index < repair_from   -> engineer decision (range not specified by operations yet)
  ud_from <= index <= ud_to     -> shock dosing (УД)
  index < ud_from               -> no measures required
Every threshold and deadline comes from settings, so the dashboard, the API and the Excel
export stay in step. The input fields are derived from source data by app.core.rating_builder.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import settings

MEASURE_REPAIR = "repair"
MEASURE_ENGINEER = "engineer"
MEASURE_UD = "ud"
MEASURE_NONE = "none"

MEASURE_LABELS = {
    MEASURE_REPAIR: "Плановый комплекс мероприятий по ремонту",
    MEASURE_ENGINEER: "Решение инженера",
    MEASURE_UD: "Ударная дозировка (УД)",
    MEASURE_NONE: "Мероприятия не требуются",
}
# Short codes for the ППР (ДДО) schedule.
MEASURE_CODES = {MEASURE_REPAIR: "ПКР", MEASURE_ENGINEER: "РИ", MEASURE_UD: "УД", MEASURE_NONE: "—"}

CATEGORY_LABELS = {
    "corrosive": "Коррозионная",
    "medium": "Среднекоррозионная",
    "noncorrosive": "Некоррозионная",
}
PROMINE_STATUS_LABELS = {
    "not_created": "Не сформирован",
    "created": "Сформирован",
    "sent": "Направлен в ЭРА",
}
PROTECTION_LABELS = {
    "effective": "Эффективна",
    "unconfirmed": "Не подтверждена",
    "insufficient": "Недостаточна",
}
PLAN_STATUS_LABELS = {
    "on_time": "В срок",
    "late": "Позже срока",
    "not_planned": "Не запланировано",
}


def thresholds() -> dict[str, int]:
    return {"repair_from": settings.rating_repair_from, "ud_from": settings.rating_ud_from,
            "ud_to": settings.rating_ud_to}


def deadline_days() -> dict[str, int]:
    return {MEASURE_REPAIR: settings.rating_days_repair, MEASURE_ENGINEER: settings.rating_days_engineer,
            MEASURE_UD: settings.rating_days_ud}


def measures_catalog() -> list[dict[str, Any]]:
    t, days = thresholds(), deadline_days()
    ranges = {
        MEASURE_REPAIR: (t["repair_from"], 100),
        MEASURE_ENGINEER: (t["ud_to"] + 1, t["repair_from"] - 1),
        MEASURE_UD: (t["ud_from"], t["ud_to"]),
        MEASURE_NONE: (0, t["ud_from"] - 1),
    }
    return [{"key": key, "label": MEASURE_LABELS[key], "code": MEASURE_CODES[key],
             "index_from": ranges[key][0], "index_to": ranges[key][1],
             "deadline_days": days.get(key), "era_required": key == MEASURE_REPAIR}
            for key in (MEASURE_REPAIR, MEASURE_ENGINEER, MEASURE_UD, MEASURE_NONE)]


def priority_index(model_ntc: float | None, model_math: float | None) -> int | None:
    scores = [value for value in (model_ntc, model_math) if value is not None]
    if not scores:
        return None
    combined = max(scores) if settings.rating_combine == "max" else sum(scores) / len(scores)
    # Round half up like Excel's ROUND, not Python's banker's rounding.
    return int(combined * 100 + 0.5 + 1e-9)


def measure_for(index: int | None) -> str | None:
    if index is None:
        return None
    if index >= settings.rating_repair_from:
        return MEASURE_REPAIR
    if index > settings.rating_ud_to:
        return MEASURE_ENGINEER
    if index >= settings.rating_ud_from:
        return MEASURE_UD
    return MEASURE_NONE


def deadline_for(measure: str | None, assessed: date | None) -> date | None:
    days = deadline_days().get(measure or "")
    if days is None or assessed is None:
        return None
    return assessed + timedelta(days=days)


def plan_status_for(measure: str | None, plan: date | None, deadline: date | None) -> str | None:
    if measure in (None, MEASURE_NONE):
        return None
    if plan is None:
        return "not_planned"
    return "late" if deadline is not None and plan > deadline else "on_time"


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    """Add the computed rating fields to one stored well_rating row."""
    index = priority_index(row.get("model_ntc"), row.get("model_math"))
    raw_ntc = row.get("model_ntc_raw")
    index_raw = priority_index(raw_ntc if raw_ntc is not None else row.get("model_ntc"), row.get("model_math"))
    measure = measure_for(index)
    # The deadline runs from the day the current measure was first assigned, so a daily
    # recomputation does not keep pushing it forward.
    assessed = _as_date(row.get("measure_since") or row.get("assessed_at"))
    deadline = deadline_for(measure, assessed)
    plan = _as_date(row.get("plan_date"))
    status = plan_status_for(measure, plan, deadline)
    return {
        **row,
        "index": index,
        # Index before the protection adjustment; differs from index only when it was applied.
        "index_raw": index_raw,
        "protection_adjusted": index is not None and index_raw is not None and index_raw != index,
        "protection_label": PROTECTION_LABELS.get(row.get("protection") or "", ""),
        "measure": measure,
        "measure_label": MEASURE_LABELS.get(measure or "", "Нет оценок моделей"),
        "measure_code": MEASURE_CODES.get(measure or "", ""),
        "era_required": measure == MEASURE_REPAIR,
        "deadline": deadline.isoformat() if deadline else None,
        "plan_date": plan.isoformat() if plan else None,
        "plan_status": status,
        "plan_status_label": PLAN_STATUS_LABELS.get(status or "", ""),
        "category_label": CATEGORY_LABELS.get(row.get("category") or "", ""),
        "promine_status_label": PROMINE_STATUS_LABELS.get(row.get("promine_status") or "", ""),
    }


def build_rating(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate and rank rows: highest index first; equal indexes share a rank (Excel RANK)."""
    evaluated = [evaluate(row) for row in rows]
    evaluated.sort(key=lambda item: (item["index"] is None, -(item["index"] or 0), str(item["well_id"])))
    scored = [item["index"] for item in evaluated if item["index"] is not None]
    for item in evaluated:
        item["rank"] = (1 + sum(1 for value in scored if value > item["index"])
                        if item["index"] is not None else None)
    return evaluated


def summarize(wells: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"total": len(wells), MEASURE_REPAIR: 0, MEASURE_ENGINEER: 0, MEASURE_UD: 0,
              MEASURE_NONE: 0, "unscored": 0, "promine_pending": 0, "plan_late": 0, "plan_missing": 0,
              "protection_adjusted": 0, "protection_insufficient": 0}
    for well in wells:
        counts[well["measure"] or "unscored"] += 1
        if well["era_required"] and well.get("promine_status") != "sent":
            counts["promine_pending"] += 1
        if well["plan_status"] == "late":
            counts["plan_late"] += 1
        if well["plan_status"] == "not_planned":
            counts["plan_missing"] += 1
        if well.get("protection_adjusted"):
            counts["protection_adjusted"] += 1
        if well.get("protection") == "insufficient":
            counts["protection_insufficient"] += 1
    return counts
