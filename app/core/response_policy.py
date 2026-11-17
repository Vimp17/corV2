"""Deterministic selection from the register using dated, original measurements."""
from __future__ import annotations

import hashlib
import math
import operator
from datetime import datetime

from app.core.normalization import as_utc
from app.core.response_rules import FIELDS, RuleBook

OPS = {"eq": operator.eq, "gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}
SYMBOLS = {"eq": "=", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}


def evaluate(book: RuleBook, points: list, *, now: datetime, mode: str,
             snapshot_ids: set[str] | None = None) -> dict:
    if mode not in {"demo", "approved"}:
        raise ValueError("MAI_RESPONSE_RULES_MODE: допустимы demo и approved")
    now = as_utc(now)
    snapshot_ids = snapshot_ids or set()
    # Preserve field ownership; do not use carried values or CO2 defaults.
    observations = {field: [] for field in FIELDS}
    for point in points:
        at = as_utc(point.timestamp)
        if at > now or (getattr(point, "synthetic", False) and mode != "demo"):
            continue
        for field in FIELDS:
            key = field.split(".")[1]
            value = getattr(point, key, None)
            source = (getattr(point, "field_sources", {}) or {}).get(key, getattr(point, "source_id", ""))
            ids = {part for part in str(source or "").split("+") if part}
            if value is None or key in getattr(point, "imputed_fields", ()) or (ids and ids <= snapshot_ids):
                continue
            observations[field].append((at, value, str(source or "источник не указан")))

    evaluated, matched, gaps = [], [], []
    for rule in book.rules:
        if rule.status != ("test" if mode == "demo" else "approved"):
            continue
        groups = {"all": [], "any": []}
        evidence = []
        for group in groups:
            for condition in getattr(rule.conditions, group):
                indicator = FIELDS[condition.field]
                records = observations[condition.field]
                latest = max((r[0] for r in records), default=None)
                records = [r for r in records if r[0] == latest]
                fresh = latest is not None and (now-latest).total_seconds() <= condition.max_age_days * 86400
                valid = fresh and all(type(r[1]) in (int, float) and math.isfinite(r[1])
                    and indicator.low <= r[1] <= indicator.high for r in records)
                conflict = valid and len({r[1] for r in records}) > 1
                available = valid and not conflict
                value = records[0][1] if available else None
                if condition.op == "is_missing":
                    # Bad/conflicting measurements are unknown, not proof that no measurement exists.
                    result = None if fresh and not available else not fresh
                else:
                    result = OPS[condition.op](value, condition.value) if available else None
                reason = (f"{indicator.name}: противоречивые измерения" if conflict else
                          f"{indicator.name}: нет достоверного измерения за {condition.max_age_days} сут.")
                if result is None:
                    gaps.append(reason)
                text = (f"{indicator.name}: {indicator.show(value)} {SYMBOLS.get(condition.op, '')} "
                        f"{indicator.show(condition.value)}; {latest.isoformat()}; "
                        + ", ".join(sorted({r[2] for r in records}))) if available and condition.op != "is_missing" else reason
                evidence.append({"field": condition.field, "group": group, "result": result,
                    "value": value, "threshold": condition.value, "operator": condition.op,
                    "timestamp": latest.isoformat() if latest else None,
                    "source": sorted({r[2] for r in records}), "text": text})
                groups[group].append(result)
        confirmed = all(v is True for v in groups["all"]) and (not groups["any"] or any(v is True for v in groups["any"]))
        item = {"id": rule.id, "revision": rule.revision, "matched": confirmed, "evidence": evidence,
                "action": rule.action, "explanation": rule.explanation}
        evaluated.append(item)
        if confirmed:
            matched.append(item)
    return {"mode": mode, "revision": book.revision,
            "sha256": hashlib.sha256(book.model_dump_json().encode()).hexdigest(),
            "evaluated_at": now.isoformat(), "matched": matched, "rules": evaluated,
            "missing_data": list(dict.fromkeys(gaps))}
