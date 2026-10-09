from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite

from app.config import settings
from app.core.quality import MEASUREMENTS
from app.schemas import FeatureSnapshot


TREND_FIELDS = (
    "water_cut_pct", "co2_pct", "chlorides_mg_l", "inhibitor_efficiency",
)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _slope(rows: list[dict], field: str, window_days: int | None = None,
           min_observations: int | None = None) -> tuple[float | None, int]:
    """Least-squares slope per day over the trailing window ending at the latest record.

    Window and minimum sample count default to MAI_TREND_WINDOW_DAYS and
    MAI_MIN_TREND_OBSERVATIONS, the same values readiness uses.
    """
    window_days = settings.trend_window_days if window_days is None else window_days
    min_observations = settings.min_trend_observations if min_observations is None else min_observations
    latest = _parse_timestamp(rows[-1]["timestamp"])
    samples: list[tuple[float, float]] = []
    for row in rows:
        ts = _parse_timestamp(row["timestamp"])
        days_before_latest = (latest - ts).total_seconds() / 86400.0
        value = row["calculated"].get(field)
        if days_before_latest <= window_days and value is not None and isfinite(float(value)):
            samples.append((-days_before_latest, float(value)))
    if len(samples) < min_observations:
        return None, len(samples)
    mean_x = sum(x for x, _ in samples) / len(samples)
    mean_y = sum(y for _, y in samples) / len(samples)
    denom = sum((x - mean_x) ** 2 for x, _ in samples)
    if denom <= 0:
        return None, len(samples)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in samples) / denom
    return (slope if isfinite(slope) else None), len(samples)


def build_features(well_id: str, normalized: list[dict]) -> FeatureSnapshot:
    if not normalized:
        raise ValueError("at least one normalized record is required")
    rows = sorted(normalized, key=lambda row: _parse_timestamp(row["timestamp"]))
    timestamps = [_parse_timestamp(row["timestamp"]) for row in rows]
    latest = timestamps[-1]
    first = timestamps[0]
    current = dict(rows[-1]["calculated"])
    trends: dict[str, float | None] = {}
    counts: dict[str, int] = {}
    for field in TREND_FIELDS:
        trends[field], counts[field] = _slope(rows, field)
    available = sum(
        1 for row in rows for field in MEASUREMENTS
        if row["raw"].get(field) is not None
    )
    possible = len(rows) * len(MEASUREMENTS)
    return FeatureSnapshot(
        well_id=well_id,
        latest_timestamp=latest,
        observation_count=len(rows),
        days_covered=max(0.0, (latest - first).total_seconds() / 86400.0),
        missing_signal_fraction=1.0 - (available / possible if possible else 0.0),
        current_values=current,
        feature_sources=dict(rows[-1].get("feature_sources", {})),
        trends_per_day=trends,
        trend_observation_count=counts,
    )
