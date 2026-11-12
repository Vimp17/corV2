"""НТЦ rule score: points for the environment, protection, technology and corrosion findings on the
current values and trends of a well (app.core.features).

The score is the sum of the points divided by CRITICAL_POINTS, capped at 1: a deterministic expert
index for the rating, not a probability of failure. The thresholds are the demonstration values of the
V3.0 notebook; calibrate them against field evidence before operational use.
"""
from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo
from datetime import timedelta

from app.core.normalization import field_observations

from app.config import settings
from app.core.features import Features
from app.core.indicators import fmt

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


GROUPS = {"ENV": "Среда", "PROT": "Защита", "TECH": "Технология", "CORR": "Коррозия"}


@dataclass(frozen=True)
class Check:
    """One rule on a well, fired or not: what it looked at, its condition and the points it gave."""

    rule_id: str
    title: str
    condition: str
    value: str | None          # the value the rule looked at; None — no data for it
    points: int = 0            # 0 when the rule did not fire
    reason: str | None = None  # why it fired
    missing: str | None = None  # why there is no value, when not simply no data

    @property
    def group(self) -> str:
        return GROUPS[self.rule_id.split("_", 1)[0]]


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
                                  f"({fmt(wall)} из {fmt(initial)} мм); порог {THRESHOLDS[key]:.0%}.")
        return None
    for critical, key, text in ((True, "wall_thickness_critical", "Остаточная толщина достигла критического порога"),
                                (False, "wall_thickness_warning", "Остаточная толщина ниже порога предупреждения")):
        if wall <= THRESHOLDS[key]:
            return critical, f"{text} ({fmt(wall)} мм); исходная толщина неизвестна, применён абсолютный порог."
    return None


def _persistent(normalized: list[dict], field: str, predicate) -> bool:
    """The field met the predicate on each of the last 7 days with a measurement (no carried values)."""
    if not normalized:
        return False
    zone = ZoneInfo(settings.source_timezone)
    latest = normalized[-1]["timestamp"].astimezone(zone).date()
    daily = {row["timestamp"].astimezone(zone).date(): row["calculated"][field]
             for row in field_observations(normalized, field)}
    return all((day := latest - timedelta(days=i)) in daily and predicate(daily[day]) for i in range(7))


def evaluate_rules(features: Features, normalized: list[dict]) -> list[Check] | None:
    """Every rule on the well — the fired ones with their points; None when the well has no current value."""
    values, trends = features.current_values, features.trends_per_day
    if not any(value is not None for value in values.values()):
        return None
    checks: list[Check] = []

    def graded(field: str, title: str, unit: str, high: str, critical: str, ids: tuple[str, str],
               reasons: tuple[str, str], below: bool = False) -> None:
        value, sign = values.get(field), "<" if below else "≥"
        condition = (f"{sign} {fmt(THRESHOLDS[high])}{unit} → +{POINTS['high']}; "
                     f"{sign} {fmt(THRESHOLDS[critical])}{unit} → +{POINTS['critical']}")
        if value is None:
            checks.append(Check(ids[0], title, condition, None))
            return
        beyond = (lambda key: value < THRESHOLDS[key]) if below else (lambda key: value >= THRESHOLDS[key])
        shown = f"{fmt(value)}{unit}"
        if beyond(critical):
            checks.append(Check(ids[1], title, condition, shown, POINTS["critical"], reasons[1]))
        elif beyond(high):
            checks.append(Check(ids[0], title, condition, shown, POINTS["high"], reasons[0]))
        else:
            checks.append(Check(ids[0], title, condition, shown))

    def growth(rule_id: str, field: str, title: str, unit: str, limit: float, reason: str) -> None:
        slope = trends.get(field)
        condition = (f"падение быстрее {fmt(-limit)}{unit} в сутки" if limit < 0 else f"рост > {fmt(limit)}{unit} в сутки"
                     ) + f" за {settings.trend_window_days} сут. → +{POINTS['growth']}"
        if slope is None:
            checks.append(Check(rule_id, title, condition, None, missing=None if values.get(field) is None else (
                f"мало замеров для тренда: нужно {settings.min_trend_observations} за {settings.trend_window_days} сут.")))
            return
        fired = slope < limit if limit < 0 else slope > limit
        checks.append(Check(rule_id, title, condition, f"{'+' if slope > 0 else ''}{fmt(slope)}{unit} в сутки",
                            POINTS["growth"] if fired else 0, reason if fired else None))

    def persistent(rule_id: str, field: str, title: str, condition: str, predicate, reason: str) -> None:
        fired = _persistent(normalized, field, predicate)
        value = "7 дней подряд" if fired else None if values.get(field) is None else "меньше 7 дней подряд"
        checks.append(Check(rule_id, title, f"{condition} 7 дней подряд → +{POINTS['persistent_7d']}", value,
                            POINTS["persistent_7d"] if fired else 0, reason if fired else None))

    graded("co2_pct", "Уровень CO₂", " %", "co2_high", "co2_critical", ("ENV_CO2_001", "ENV_CO2_003"),
           ("Повышенный CO₂", "Критический уровень CO₂"))
    growth("ENV_CO2_002", "co2_pct", "Рост CO₂", " %", 0.012, "Наблюдается рост CO₂")
    persistent("ENV_CO2_004", "co2_pct", "CO₂ держится выше порога", f"≥ {fmt(THRESHOLDS['co2_high'])} %",
               lambda x: x >= THRESHOLDS["co2_high"], "CO₂ превышает высокий порог не менее 7 дней")

    graded("water_cut_pct", "Обводнённость", " %", "water_cut_high", "water_cut_critical",
           ("ENV_WATER_001", "ENV_WATER_003"), ("Высокая обводнённость", "Критическая обводнённость"))
    growth("ENV_WATER_002", "water_cut_pct", "Рост обводнённости", " %", 0.10, "Наблюдается рост обводнённости")
    persistent("ENV_WATER_004", "water_cut_pct", "Обводнённость держится выше порога",
               f"≥ {fmt(THRESHOLDS['water_cut_high'])} %", lambda x: x >= THRESHOLDS["water_cut_high"],
               "Высокая обводнённость сохраняется не менее 7 дней")

    graded("chlorides_mg_l", "Хлориды", " мг/л", "chlorides_high", "chlorides_critical", ("ENV_CL_001", "ENV_CL_002"),
           ("Высокое содержание хлоридов", "Критическое содержание хлоридов"))
    persistent("ENV_CL_003", "chlorides_mg_l", "Хлориды держатся выше порога",
               f"≥ {fmt(THRESHOLDS['chlorides_high'])} мг/л", lambda x: x >= THRESHOLDS["chlorides_high"],
               "Высокое содержание хлоридов сохраняется не менее 7 дней")

    graded("inhibitor_efficiency", "Эффективность ингибитора", "", "inhibitor_low", "inhibitor_critical",
           ("PROT_INH_001", "PROT_INH_002"),
           ("Эффективность ингибиторной защиты ниже порога", "Критически низкая эффективность ингибиторной защиты"),
           below=True)
    growth("PROT_INH_003", "inhibitor_efficiency", "Снижение эффективности ингибитора", "", -0.0025,
           "Эффективность защиты устойчиво снижается")
    persistent("PROT_INH_004", "inhibitor_efficiency", "Ингибитор держится ниже порога",
               f"< {fmt(THRESHOLDS['inhibitor_low'])}", lambda x: x < THRESHOLDS["inhibitor_low"],
               "Недостаточная защита сохраняется не менее 7 дней")

    graded("injection_deviation_pct", "Отклонение закачки ингибитора", " %", "injection_dev_high",
           "injection_dev_critical", ("TECH_INJ_001", "TECH_INJ_002"),
           ("Отклонение режима закачки превышает порог", "Критическое отклонение режима закачки"))
    persistent("TECH_INJ_003", "injection_deviation_pct", "Отклонение закачки держится выше порога",
               f"≥ {fmt(THRESHOLDS['injection_dev_high'])} %", lambda x: x >= THRESHOLDS["injection_dev_high"],
               "Отклонение режима закачки сохраняется не менее 7 дней")
    issue = values.get("water_compatibility_issue")
    checks.append(Check("TECH_COMP_001", "Несовместимость вод", f"признак «да» → +{POINTS['compatibility']}",
                        None if issue is None else "да" if issue else "нет",
                        POINTS["compatibility"] if issue is True else 0,
                        "Обнаружен признак несовместимости вод" if issue is True else None))

    graded("corrosion_rate_mm_year", "Скорость коррозии", " мм/год", "corrosion_rate_high", "corrosion_rate_critical",
           ("CORR_RATE_001", "CORR_RATE_002"), ("Высокая скорость коррозии", "Критическая скорость коррозии"))
    persistent("CORR_RATE_003", "corrosion_rate_mm_year", "Скорость коррозии держится выше порога",
               f"≥ {fmt(THRESHOLDS['corrosion_rate_high'])} мм/год", lambda x: x >= THRESHOLDS["corrosion_rate_high"],
               "Высокая скорость коррозии сохраняется не менее 7 дней")
    wall, initial = values.get("wall_thickness_mm"), values.get("initial_wall_thickness_mm")
    condition = (f"потеря ≥ {THRESHOLDS['wall_loss_warning']:.0%} начальной толщины (без неё ≤ "
                 f"{fmt(THRESHOLDS['wall_thickness_warning'])} мм) → +{POINTS['wall_warning']}; ≥ "
                 f"{THRESHOLDS['wall_loss_critical']:.0%} (≤ {fmt(THRESHOLDS['wall_thickness_critical'])} мм) → "
                 f"+{POINTS['wall_critical']}")
    shown = None if wall is None else (f"{fmt(wall)} из {fmt(initial)} мм, потеря {(initial - wall) / initial:.0%}"
                                       if initial and initial >= wall else f"{fmt(wall)} мм")
    thin = assess_wall(values)
    if thin is None:
        checks.append(Check("CORR_WALL_001", "Толщина стенки НКТ", condition, shown))
    else:
        critical, reason = thin
        checks.append(Check("CORR_WALL_002" if critical else "CORR_WALL_001", "Толщина стенки НКТ", condition, shown,
                            POINTS["wall_critical"] if critical else POINTS["wall_warning"], reason))
    return checks


def score(points: float) -> float:
    """Rule points as the normalised НТЦ score (0–1)."""
    return round(min(points / CRITICAL_POINTS, 1.0), 4)
