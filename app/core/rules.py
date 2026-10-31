"""НТЦ rule score: points for the environment, protection, technology and corrosion findings on the
current values and trends of a well (app.core.features).

The score is the sum of the points divided by CRITICAL_POINTS, capped at 1: a deterministic expert
index for the rating, not a probability of failure. The thresholds are the demonstration values of the
V3.0 notebook; calibrate them against field evidence before operational use.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.core.features import Features

THRESHOLDS = {
    "water_cut_high": 75.0, "water_cut_critical": 90.0,
    "co2_high": 3.0, "co2_critical": 5.0,
    "chlorides_high": 30000.0, "chlorides_critical": 50000.0,
    "inhibitor_low": 0.70, "inhibitor_critical": 0.40,
    "injection_dev_high": 15.0, "injection_dev_critical": 30.0,
    "wall_thickness_warning": 8.0, "wall_thickness_critical": 5.0,
    # Fraction of the initial wall lost. Chosen to match the absolute thresholds above for the
    # 12 mm demo pipe (8 mm left = 1/3 lost, 5 mm left = 7/12 lost); used whenever the initial
    # thickness is known, so thin- and thick-walled pipes are judged by their own design wall.
    "wall_loss_warning": 0.33, "wall_loss_critical": 0.58,
    "corrosion_rate_high": 0.35, "corrosion_rate_critical": 0.60,
}
POINTS = {"high": 4, "critical": 7, "growth": 1, "persistent_7d": 1,
          "compatibility": 7, "wall_warning": 2, "wall_critical": 14}
CRITICAL_POINTS = 14
# Rules on the environment (CO₂, water cut, chlorides): the points protection may reduce in the rating.
ENVIRONMENT_PREFIX = "ENV_"


@dataclass(frozen=True)
class Finding:
    rule_id: str
    points: int
    reason: str


def assess_wall(values: dict) -> tuple[bool, str] | None:
    """(critical, reason) for a thin wall, judged against the initial wall when it is known; None
    when the wall is fine or not measured."""
    wall = values.get("wall_thickness_mm")
    if wall is None:
        return None
    initial = values.get("initial_wall_thickness_mm")
    if initial is not None and initial > 0 and initial >= wall:
        loss = (initial - wall) / initial
        for critical, key in ((True, "wall_loss_critical"), (False, "wall_loss_warning")):
            if loss >= THRESHOLDS[key]:
                return critical, (f"Потеряно {loss:.0%} исходной толщины стенки "
                                  f"({wall:g} из {initial:g} мм); порог {THRESHOLDS[key]:.0%}.")
        return None
    for critical, key, text in ((True, "wall_thickness_critical", "Остаточная толщина достигла критического порога"),
                                (False, "wall_thickness_warning", "Остаточная толщина ниже порога предупреждения")):
        if wall <= THRESHOLDS[key]:
            return critical, f"{text} ({wall:g} мм); исходная толщина неизвестна, применён абсолютный порог."
    return None


def _persistent(normalized: list[dict], field: str, predicate) -> bool:
    """The field met the predicate on each of the last 7 days with a measurement (no carried values)."""
    if not normalized:
        return False
    latest = normalized[-1]["timestamp"]
    window = []
    for row in reversed(normalized):
        timestamp = row["timestamp"]
        if latest - timestamp > timedelta(days=6):
            break
        if timestamp.date() not in {item[0] for item in window}:
            if field in row.get("imputed_fields", []):
                return False
            window.append((timestamp.date(), row["calculated"].get(field)))
    return len(window) >= 7 and all(value is not None and predicate(value) for _, value in window[:7])


def evaluate_rules(features: Features, normalized: list[dict]) -> list[Finding] | None:
    """The triggered rules; None when the well has no current value at all."""
    values, trends = features.current_values, features.trends_per_day
    if not any(value is not None for value in values.values()):
        return None
    findings: list[Finding] = []

    def add(rule_id: str, condition: bool, points: int, reason: str) -> None:
        if condition:
            findings.append(Finding(rule_id, points, reason))

    def graded(field: str, high: str, critical: str, ids: tuple[str, str], reasons: tuple[str, str],
               below: bool = False) -> None:
        value = values.get(field)
        if value is None:
            return
        beyond = (lambda key: value < THRESHOLDS[key]) if below else (lambda key: value >= THRESHOLDS[key])
        if beyond(critical):
            add(ids[1], True, POINTS["critical"], reasons[1])
        elif beyond(high):
            add(ids[0], True, POINTS["high"], reasons[0])

    def growth(rule_id: str, field: str, limit: float, reason: str) -> None:
        slope = trends.get(field)
        add(rule_id, slope is not None and (slope < limit if limit < 0 else slope > limit), POINTS["growth"], reason)

    def persistent(rule_id: str, field: str, predicate, reason: str) -> None:
        add(rule_id, _persistent(normalized, field, predicate), POINTS["persistent_7d"], reason)

    graded("co2_pct", "co2_high", "co2_critical", ("ENV_CO2_001", "ENV_CO2_003"),
           ("Повышенный CO₂", "Критический уровень CO₂"))
    growth("ENV_CO2_002", "co2_pct", 0.012, "Наблюдается рост CO₂")
    persistent("ENV_CO2_004", "co2_pct", lambda x: x >= THRESHOLDS["co2_high"],
               "CO₂ превышает высокий порог не менее 7 дней")

    graded("water_cut_pct", "water_cut_high", "water_cut_critical", ("ENV_WATER_001", "ENV_WATER_003"),
           ("Высокая обводнённость", "Критическая обводнённость"))
    growth("ENV_WATER_002", "water_cut_pct", 0.10, "Наблюдается рост обводнённости")
    persistent("ENV_WATER_004", "water_cut_pct", lambda x: x >= THRESHOLDS["water_cut_high"],
               "Высокая обводнённость сохраняется не менее 7 дней")

    graded("chlorides_mg_l", "chlorides_high", "chlorides_critical", ("ENV_CL_001", "ENV_CL_002"),
           ("Высокое содержание хлоридов", "Критическое содержание хлоридов"))
    persistent("ENV_CL_003", "chlorides_mg_l", lambda x: x >= THRESHOLDS["chlorides_high"],
               "Высокое содержание хлоридов сохраняется не менее 7 дней")

    graded("inhibitor_efficiency", "inhibitor_low", "inhibitor_critical", ("PROT_INH_001", "PROT_INH_002"),
           ("Эффективность ингибиторной защиты ниже порога", "Критически низкая эффективность ингибиторной защиты"),
           below=True)
    growth("PROT_INH_003", "inhibitor_efficiency", -0.0025, "Эффективность защиты устойчиво снижается")
    persistent("PROT_INH_004", "inhibitor_efficiency", lambda x: x < THRESHOLDS["inhibitor_low"],
               "Недостаточная защита сохраняется не менее 7 дней")

    graded("injection_deviation_pct", "injection_dev_high", "injection_dev_critical", ("TECH_INJ_001", "TECH_INJ_002"),
           ("Отклонение режима закачки превышает порог", "Критическое отклонение режима закачки"))
    persistent("TECH_INJ_003", "injection_deviation_pct", lambda x: x >= THRESHOLDS["injection_dev_high"],
               "Отклонение режима закачки сохраняется не менее 7 дней")
    add("TECH_COMP_001", values.get("water_compatibility_issue") is True, POINTS["compatibility"],
        "Обнаружен признак несовместимости вод")

    graded("corrosion_rate_mm_year", "corrosion_rate_high", "corrosion_rate_critical", ("CORR_RATE_001", "CORR_RATE_002"),
           ("Высокая скорость коррозии", "Критическая скорость коррозии"))
    persistent("CORR_RATE_003", "corrosion_rate_mm_year", lambda x: x >= THRESHOLDS["corrosion_rate_high"],
               "Высокая скорость коррозии сохраняется не менее 7 дней")
    wall = assess_wall(values)
    if wall is not None:
        critical, reason = wall
        add("CORR_WALL_002" if critical else "CORR_WALL_001", True,
            POINTS["wall_critical"] if critical else POINTS["wall_warning"], reason)
    return findings


def score(points: float) -> float:
    """Rule points as the normalised НТЦ score (0–1)."""
    return round(min(points / CRITICAL_POINTS, 1.0), 4)
