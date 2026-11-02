from __future__ import annotations

from datetime import datetime, timezone

from app.core.source_kinds import is_snapshot, point_sources
from app.schemas import TelemetryPoint

# Physical sanity bounds inherited from the V3.0 demonstration notebook: a value outside them is not
# used. They are data-quality bounds, not corrosion intervention limits.
RANGE_LIMITS: dict[str, tuple[float, float]] = {
    "water_cut_pct": (0.0, 100.0),
    "co2_pct": (0.0, 8.0),
    "chlorides_mg_l": (0.0, 80000.0),
    "inhibitor_efficiency": (0.0, 1.2),
    "injection_deviation_pct": (0.0, 60.0),
    "corrosion_rate_mm_year": (0.0, 1.5),
    "wall_thickness_mm": (0.8, 14.0),
    "initial_wall_thickness_mm": (0.8, 14.0),
    "metal_loss_mm": (0.0, 14.0),
    "corrosion_load": (0.02, 3.0),
}


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
