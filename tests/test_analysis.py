"""Source kinds in the analysis: laboratory samples stay current for the sampling window,
registry snapshots give values but are not measurements. No database needed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import tests.conftest  # noqa: F401  (settings environment)

from app.core.features import build_features
from app.core.normalization import normalize_records
from app.core.readiness import assess_readiness
from app.schemas import TelemetryPoint

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _point(days_ago: float, source: str, **values) -> TelemetryPoint:
    return TelemetryPoint.model_validate({"timestamp": NOW - timedelta(days=days_ago), "source_id": source, **values})


def _daily(days: int = 12) -> list[TelemetryPoint]:
    return [_point(day, "DARS", water_cut_pct=40, co2_pct=0.5) for day in range(days, -1, -1)]


def test_lab_value_is_carried_for_the_sampling_window():
    records = sorted(_daily() + [_point(10, "KHIMIK", chlorides_mg_l=60000),
                                 _point(10, "DARS_OLD", inhibitor_efficiency=0.9)],
                     key=lambda point: point.timestamp)
    normalized, report = normalize_records(records, 7, sample_fill_days=30, sample_sources={"KHIMIK"})
    current = build_features("W", normalized).current_values
    assert current["chlorides_mg_l"] == 60000          # sample 10 days old still counts (30-day window)
    assert current["inhibitor_efficiency"] is None     # telemetry value 10 days old has expired (7 days)
    assert report.sample_fill_limit_days == 30
    # Without the sample sources the old behaviour stays: 7 days for every value.
    normalized, _ = normalize_records(records, 7)
    assert build_features("W", normalized).current_values["chlorides_mg_l"] is None


def test_registry_snapshot_is_not_a_measurement():
    records = _daily() + [_point(-0.1, "DOC_NKT", water_cut_pct=95, chlorides_mg_l=30000)]
    normalized, _ = normalize_records(records, 7, sample_fill_days=30, sample_sources={"DOC_NKT"},
                                      snapshot_sources={"DOC_NKT"})
    features = build_features("W", normalized)
    assert features.latest_timestamp == NOW                   # the snapshot does not move «latest»
    assert features.observation_count == 13
    assert features.current_values["water_cut_pct"] == 95     # but its newer values are current
    assert features.current_values["chlorides_mg_l"] == 30000


def test_readiness_ignores_registry_rows():
    only_registry = [_point(day, "DOC_NKT", water_cut_pct=90, co2_pct=1.0) for day in range(10)]
    skipped = assess_readiness("W", only_registry, [])
    assert skipped is not None and skipped.telemetry_records == 0
    assert assess_readiness("W", _daily(), []) is None


def test_deadline_counts_from_the_local_day():
    from app.core import rating

    # 01:30 Moscow time on 21.10 is still 20.10 in UTC: the measure counts from 21.10.
    assert str(rating._as_date("2026-10-20T22:30:00+00:00")) == "2026-10-21"
    assert str(rating._as_date("2026-10-20")) == "2026-10-20"
