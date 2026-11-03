"""Source kinds in the analysis: laboratory samples stay current for the sampling window,
registry snapshots give values but are not measurements. No database needed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import tests.conftest  # noqa: F401  (settings environment)

from app.core.features import build_features
from app.core.normalization import normalize_records
from app.core.readiness import assess
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
    current = build_features(normalize_records(records, 7, sample_fill_days=30,
                                               sample_sources={"KHIMIK"})).current_values
    assert current["chlorides_mg_l"] == 60000          # sample 10 days old still counts (30-day window)
    assert current["inhibitor_efficiency"] is None     # telemetry value 10 days old has expired (7 days)
    # Without the sample sources the old behaviour stays: 7 days for every value.
    assert build_features(normalize_records(records, 7)).current_values["chlorides_mg_l"] is None


def test_registry_snapshot_is_not_a_measurement():
    records = _daily() + [_point(-0.1, "DOC_NKT", water_cut_pct=95, chlorides_mg_l=30000)]
    features = build_features(normalize_records(records, 7, sample_fill_days=30, sample_sources={"DOC_NKT"},
                                                snapshot_sources={"DOC_NKT"}))
    assert features.latest_timestamp == NOW                   # the snapshot does not move «latest»
    assert features.observation_count == 13
    assert features.current_values["water_cut_pct"] == 95     # but its newer values are current
    assert features.current_values["chlorides_mg_l"] == 30000


def test_readiness_ignores_registry_rows():
    only_registry = [_point(day, "DOC_NKT", water_cut_pct=90, co2_pct=1.0) for day in range(10)]
    readiness = assess(only_registry)
    assert not readiness.ready and readiness.unique_timestamps == 0
    assert assess(_daily()).ready


def test_deadline_counts_from_the_local_day():
    from app.core import rating

    # 01:30 Moscow time on 21.10 is still 20.10 in UTC: the measure counts from 21.10.
    assert str(rating._as_date("2026-10-20T22:30:00+00:00")) == "2026-10-21"
    assert str(rating._as_date("2026-10-20")) == "2026-10-20"


def test_every_rule_is_reported_fired_or_not():
    from app.core.rules import evaluate_rules

    records = [_point(day, "DARS", water_cut_pct=92, co2_pct=1.0, chlorides_mg_l=35000) for day in range(12, -1, -1)]
    normalized = normalize_records(records, 7)
    checks = evaluate_rules(build_features(normalized), normalized)
    assert len(checks) == 17
    fired = {check.title: check.points for check in checks if check.points}
    assert fired == {"Обводнённость": 7, "Обводнённость держится выше порога": 1, "Хлориды": 4,
                     "Хлориды держатся выше порога": 1}
    passed = {check.title: check.value for check in checks if not check.points and check.value is not None}
    assert passed["Уровень CO₂"] == "1 %" and passed["Рост обводнённости"].endswith("% в сутки")
    no_data = {check.title for check in checks if check.value is None}
    assert {"Эффективность ингибитора", "Скорость коррозии", "Толщина стенки НКТ", "Несовместимость вод"} <= no_data
    assert {check.group for check in checks} == {"Среда", "Защита", "Технология", "Коррозия"}


def test_math_model_probability_and_the_median_it_took():
    import json
    import math

    from app.config import settings
    from app.core import math_model

    raw = json.loads(settings.math_model_path.read_text(encoding="utf-8"))
    values = {"corrosion_rate_mm_year": 0.65, "co2_pct": 5.6, "chlorides_mg_l": 24000, "inhibitor_efficiency": 0.82,
              "water_cut_pct": 68}
    forecast = math_model.predict(values, {})
    linear = sum(raw["coefficients"][key] * (values[key] - raw["means"][key]) for key in raw["features"])
    # S₀ at the horizon: the last baseline point not after it (survival only goes down).
    survival = min(item["survival"] for item in raw["baseline_survival"] if item["time_days"] <= settings.rating_horizon_days)
    assert survival == forecast.baseline_survival == 0.85
    assert forecast.probability == round(1 - survival ** math.exp(linear), 4)
    assert forecast.horizon_days == 90 and forecast.imputed == []
    # A missing input takes the median of the model, and the forecast names it.
    partial = math_model.predict({key: value for key, value in values.items() if key != "co2_pct"}, {})
    assert partial.imputed == ["CO₂"]
    assert next(item for item in partial.inputs if item["key"] == "co2_pct")["value"] == raw["medians"]["co2_pct"]


def test_math_model_unavailable_without_its_file_or_beyond_its_horizon(monkeypatch, tmp_path):
    import dataclasses

    import pytest

    from app.core import math_model

    original = math_model.settings
    monkeypatch.setattr(math_model, "settings", dataclasses.replace(original, math_model_path=tmp_path / "none.json"))
    with pytest.raises(math_model.Unavailable, match="нет файла модели"):
        math_model.predict({}, {})
    monkeypatch.setattr(math_model, "settings", dataclasses.replace(original, rating_horizon_days=3000))
    with pytest.raises(math_model.Unavailable, match="горизонт 3000 сут."):
        math_model.predict({}, {})


def test_index_combines_the_two_forecasts(monkeypatch):
    import dataclasses

    from app.core import rating

    assert rating.priority_index(0.5, 0.735) == 74          # the larger one by default (MAI_RATING_COMBINE=max)
    assert rating.priority_index(None, 0.4) == 40 and rating.priority_index(None, None) is None
    monkeypatch.setattr(rating, "settings", dataclasses.replace(rating.settings, rating_combine="mean"))
    assert rating.priority_index(0.5, 0.7) == 60


def test_numbers_are_shown_the_russian_way():
    from app.core.indicators import INDICATORS, fmt, plural

    assert [fmt(value) for value in (5.627, 23990, 0.012, -4.74e-05, 12.0, 0.35)] == [
        "5,63", "23 990", "0,012", "−0,0000474", "12", "0,35"]
    assert INDICATORS["chlorides_mg_l"].show(30000) == "30 000 мг/л"
    assert [plural(count, "замер", "замера", "замеров") for count in (1, 3, 8, 11, 22)] == [
        "замер", "замера", "замеров", "замеров", "замера"]
