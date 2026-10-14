"""Well rating: priority index from the НТЦ and math model scores and the measure it implies.

The rules mirror the approved Excel template (sheet «Параметры»):
  index >= repair_from          -> planned repair package, well goes to the ERA repair plan
  ud_to < index < repair_from   -> engineer decision (range not specified by operations yet)
  ud_from <= index <= ud_to     -> shock dosing (УД)
  index < ud_from               -> no measures required
Every threshold and deadline comes from settings, so the dashboard, the API and the Excel
template stay in step.
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
    measure = measure_for(index)
    assessed = _as_date(row.get("assessed_at"))
    deadline = deadline_for(measure, assessed)
    plan = _as_date(row.get("plan_date"))
    status = plan_status_for(measure, plan, deadline)
    return {
        **row,
        "index": index,
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
              MEASURE_NONE: 0, "unscored": 0, "promine_pending": 0, "plan_late": 0, "plan_missing": 0}
    for well in wells:
        counts[well["measure"] or "unscored"] += 1
        if well["era_required"] and well.get("promine_status") != "sent":
            counts["promine_pending"] += 1
        if well["plan_status"] == "late":
            counts["plan_late"] += 1
        if well["plan_status"] == "not_planned":
            counts["plan_missing"] += 1
    return counts


# --------------------------------------------------------------------------- parsing input values
# Excel uploads carry the template's Russian labels; the JSON API accepts canonical values.
# Both pass through these parsers so stored values are always canonical.

_EMPTY = {"", "—", "-", "–", "н/д", "нет данных", "none", "null"}


def _key(value: Any) -> str:
    return str(value).strip().casefold().replace("ё", "е")


def is_blank(value: Any) -> bool:
    return value is None or _key(value) in _EMPTY


def parse_bool(value: Any, true_words: set[str], false_words: set[str], field: str) -> bool | None:
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    key = _key(value)
    if key in true_words or key in {"да", "true", "yes", "1"}:
        return True
    if key in false_words or key in {"нет", "false", "no", "0"}:
        return False
    raise ValueError(f"{field}: unrecognised value '{value}'")


def parse_in_operation(value: Any) -> bool | None:
    return parse_bool(value, {"в работе", "работает", "in_operation"}, {"не в работе", "остановлена", "stopped"},
                      "Статус")


def parse_environment(value: Any) -> bool | None:
    return parse_bool(value, {"коррозионная", "corrosive"}, {"некоррозионная", "noncorrosive"}, "Среда")


def parse_presence(value: Any, field: str) -> bool | None:
    return parse_bool(value, {"есть", "имеется", "present"}, {"отсутствует", "absent"}, field)


def parse_base_result(value: Any) -> bool | None:
    return parse_bool(value, {"коррозия есть", "есть коррозия", "есть", "corrosion"},
                      {"коррозии нет", "нет коррозии", "no_corrosion"}, "Результат разбора")


_CATEGORY_WORDS = {
    "коррозионная": "corrosive", "corrosive": "corrosive",
    "среднекоррозионная": "medium", "средне-коррозионная": "medium", "medium": "medium",
    "некоррозионная": "noncorrosive", "noncorrosive": "noncorrosive",
}
_PROMINE_WORDS = {
    "не сформирован": "not_created", "not_created": "not_created",
    "сформирован": "created", "created": "created",
    "направлен в эра": "sent", "направлен": "sent", "sent": "sent",
}


def parse_category(value: Any) -> str | None:
    if is_blank(value):
        return None
    try:
        return _CATEGORY_WORDS[_key(value)]
    except KeyError as exc:
        raise ValueError(f"Категория скважины: unrecognised value '{value}'") from exc


def parse_promine_status(value: Any) -> str | None:
    if is_blank(value):
        return None
    try:
        return _PROMINE_WORDS[_key(value)]
    except KeyError as exc:
        raise ValueError(f"Документ Промайн: unrecognised value '{value}'") from exc


def parse_number(value: Any, field: str, minimum: float | None = None,
                 maximum: float | None = None) -> float | None:
    if is_blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field}: number expected")
    number = float(value) if isinstance(value, (int, float)) else float(
        str(value).strip().replace(" ", "").replace(" ", "").replace(",", "."))
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{field}: number expected")
    if minimum is not None and number < minimum or maximum is not None and number > maximum:
        raise ValueError(f"{field}: {number:g} is outside {minimum:g}..{maximum:g}")
    return number


def parse_date(value: Any, field: str) -> date | None:
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{field}: date expected (ДД.ММ.ГГГГ or ISO), got '{value}'")


def parse_text(value: Any, limit: int) -> str | None:
    if is_blank(value):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def parse_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one mapped spreadsheet row (canonical keys, raw cell values) to canonical values.

    Only keys present in `row` are returned, so a sheet without a column never clears it.
    """
    parsers = {
        "field": lambda v: parse_text(v, 256),
        "equipment": lambda v: parse_text(v, 256),
        "in_operation": parse_in_operation,
        "sampling": lambda v: parse_bool(v, set(), set(), "Отбор проб"),
        "corrosive_environment": parse_environment,
        "tonnage_t": lambda v: parse_number(v, "Тоннаж", 0, 10000),
        "noo_days": lambda v: parse_number(v, "НОО", 0, 100000),
        "demount_act": lambda v: parse_presence(v, "Акт демонтажа"),
        "demount_corrosion": lambda v: parse_presence(v, "Признак коррозии"),
        "base_act": lambda v: parse_presence(v, "Акт разбора на базе"),
        "base_corrosion": parse_base_result,
        "category": parse_category,
        "model_ntc": lambda v: parse_number(v, "Модель НТЦ", 0, 1),
        "model_math": lambda v: parse_number(v, "Мат. модель", 0, 1),
        "plan_date": lambda v: parse_date(v, "План ППР (ДДО)"),
        "promine_status": parse_promine_status,
        "comment": lambda v: parse_text(v, 2000),
        "assessed_at": lambda v: parse_date(v, "Дата оценки"),
    }
    return {key: parser(row[key]) for key, parser in parsers.items() if key in row}
