from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from app.config import settings

TREND_FIELDS = ("water_cut_pct", "co2_pct", "chlorides_mg_l", "inhibitor_efficiency")


@dataclass
class Features:
    latest_timestamp: datetime
    observation_count: int
    current_values: dict[str, float | bool | None]
    trends_per_day: dict[str, float | None]


def _slope(rows: list[dict], field: str) -> float | None:
    """Least-squares slope per day over the trend window ending at the latest row: the window and the
    minimum number of values are MAI_TREND_WINDOW_DAYS and MAI_MIN_TREND_OBSERVATIONS, as in readiness."""
    latest = rows[-1]["timestamp"]
    samples: list[tuple[float, float]] = []
    for row in rows:
        days_before_latest = (latest - row["timestamp"]).total_seconds() / 86400.0
        value = row["calculated"].get(field)
        if days_before_latest <= settings.trend_window_days and value is not None and isfinite(float(value)):
            samples.append((-days_before_latest, float(value)))
    if len(samples) < settings.min_trend_observations:
        return None
    mean_x = sum(x for x, _ in samples) / len(samples)
    mean_y = sum(y for _, y in samples) / len(samples)
    denom = sum((x - mean_x) ** 2 for x, _ in samples)
    if denom <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in samples) / denom
    return slope if isfinite(slope) else None


def build_features(normalized: list[dict]) -> Features:
    """Current values and trends of a well from its normalised rows (app.core.normalization).

    Rows marked "snapshot" (a registry without its own measurement time) are not observations:
    the latest time and the trends come from the other rows. A snapshot newer than the latest
    observation still updates the current values it carries."""
    if not normalized:
        raise ValueError("at least one normalized record is required")
    every = sorted(normalized, key=lambda row: row["timestamp"])
    rows = [row for row in every if not row.get("snapshot")] or every
    latest = rows[-1]["timestamp"]
    current = dict(rows[-1]["calculated"])
    for row in every:
        if row.get("snapshot") and row["timestamp"] > latest:
            for field, value in row["calculated"].items():
                if value is not None and field not in row.get("imputed_fields", ()):
                    current[field] = value
    return Features(latest_timestamp=latest, observation_count=len(rows), current_values=current,
                    trends_per_day={field: _slope(rows, field) for field in TREND_FIELDS})
