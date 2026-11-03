from __future__ import annotations

from datetime import datetime, timezone

from app.core.indicators import INDICATORS
from app.core.source_kinds import is_snapshot, point_sources
from app.schemas import TelemetryPoint

# A value outside the sanity bounds of its indicator (app.core.indicators) is not used.
RANGE_LIMITS: dict[str, tuple[float, float]] = {key: (item.low, item.high) for key, item in INDICATORS.items()}


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def normalize_records(records: list[TelemetryPoint], max_forward_fill_days: int,
                      sample_fill_days: int | None = None, sample_sources: set[str] | None = None,
                      snapshot_sources: set[str] | None = None) -> list[dict]:
    """The records in time order, range-checked, with each field's last valid value carried forward.

    A value is carried max_forward_fill_days; a value that came from a laboratory sample or a snapshot
    (sample_sources) is carried sample_fill_days, the window in which the sample counts. Rows from
    snapshot_sources only (a registry without its own measurement time) are marked "snapshot": they
    give values but are not observations (app.core.source_kinds). A row: timestamp (UTC), calculated
    (the value of each field, None when there is none), imputed_fields (carried values), snapshot."""
    sample_sources = sample_sources or set()
    snapshot_sources = snapshot_sources or set()
    long_fill = max(sample_fill_days or 0, max_forward_fill_days)
    # field -> (time, value, carried for how many days)
    last_valid: dict[str, tuple[datetime, float, int]] = {}
    rows: list[dict] = []
    for point in sorted(records, key=lambda item: as_utc(item.timestamp)):
        timestamp = as_utc(point.timestamp)
        fill_days = long_fill if point_sources(point) & sample_sources else max_forward_fill_days
        row = {"timestamp": timestamp, "calculated": {}, "imputed_fields": [],
               "snapshot": is_snapshot(point, snapshot_sources)}
        for field, (low, high) in RANGE_LIMITS.items():
            value = getattr(point, field)
            if value is not None and low <= value <= high:
                row["calculated"][field] = float(value)
                last_valid[field] = (timestamp, float(value), fill_days)
                continue
            previous = last_valid.get(field)
            if previous and (timestamp - previous[0]).total_seconds() <= previous[2] * 86400:
                row["calculated"][field] = previous[1]
                row["imputed_fields"].append(field)
            else:
                row["calculated"][field] = None
        row["calculated"]["water_compatibility_issue"] = point.water_compatibility_issue
        rows.append(row)
    return rows
