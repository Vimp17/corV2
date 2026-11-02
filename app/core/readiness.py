"""Whether a well has enough measurements for the НТЦ rule score.

Needed: MAI_MIN_TELEMETRY_RECORDS measurements with different times, MAI_MIN_TREND_OBSERVATIONS of
them within the trend window (MAI_TREND_WINDOW_DAYS) and MAI_MIN_CURRENT_SIGNALS model inputs measured
within MAI_MAX_FORWARD_FILL_DAYS of the latest one; the latest may not be ahead of the clock by more
than MAI_MAX_FUTURE_SKEW_HOURS (a clock or time-zone error). Stale data do not block the score: the
rating keeps the last known risk and its data gaps show the dates. Snapshots (a registry without its
own measurement time) are not measurements and are left out.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.core.normalization import RANGE_LIMITS, as_utc
from app.core.source_kinds import observations
from app.schemas import TelemetryPoint

# Model inputs by name; the first MAIN_SIGNALS are measured routinely, the rest are rare.
SIGNAL_NAMES = {
    "water_cut_pct": "обводнённость", "co2_pct": "CO₂", "chlorides_mg_l": "хлориды",
    "inhibitor_efficiency": "эффективность ингибитора", "injection_deviation_pct": "отклонение закачки",
    "corrosion_rate_mm_year": "скорость коррозии", "wall_thickness_mm": "толщина стенки",
    "initial_wall_thickness_mm": "начальная толщина стенки", "metal_loss_mm": "потеря металла",
    "corrosion_load": "коррозионная нагрузка",
}
MAIN_SIGNALS = 7


@dataclass
class Readiness:
    unique_timestamps: int
    recent_observations: int
    # Model inputs with a valid value within MAI_MAX_FORWARD_FILL_DAYS of the latest measurement.
    current_signals: list[str]
    # Hours the latest measurement is ahead of the clock, when beyond MAI_MAX_FUTURE_SKEW_HOURS.
    ahead_hours: float | None = None

    @property
    def ready(self) -> bool:
        return (self.unique_timestamps >= settings.min_telemetry_records
                and self.recent_observations >= settings.min_trend_observations
                and len(self.current_signals) >= settings.min_current_signals
                and self.ahead_hours is None)


def assess(records: list[TelemetryPoint]) -> Readiness:
    points = observations(records)
    moments = [as_utc(point.timestamp) for point in points]
    latest = max(moments, default=None)
    recent: set[datetime] = set()
    current: set[str] = set()
    for point, moment in zip(points, moments):
        if latest - moment <= timedelta(days=settings.trend_window_days):
            recent.add(moment)
        if latest - moment <= timedelta(days=settings.max_forward_fill_days):
            for field in SIGNAL_NAMES:
                value = getattr(point, field)
                low, high = RANGE_LIMITS[field]
                if value is not None and low <= value <= high:
                    current.add(field)
    ahead = (latest - datetime.now(timezone.utc)).total_seconds() / 3600 if latest is not None else None
    return Readiness(unique_timestamps=len(set(moments)), recent_observations=len(recent),
                     current_signals=[field for field in SIGNAL_NAMES if field in current],
                     ahead_hours=ahead if ahead is not None and ahead > settings.max_future_skew_hours else None)
