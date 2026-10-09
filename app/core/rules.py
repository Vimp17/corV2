from __future__ import annotations

from datetime import datetime, timezone

from app.core.diagnostics import THRESHOLDS, assess_wall
from app.schemas import ActionItem, FeatureSnapshot, RuleFinding, RuleRisk


RULE_POINTS = {"high": 4, "critical": 7, "growth": 1, "persistent_7d": 1,
               "compatibility": 7, "wall_warning": 2, "wall_critical": 14}


def _persistent(normalized: list[dict], field: str, predicate) -> bool:
    if not normalized:
        return False
    latest = datetime.fromisoformat(normalized[-1]["timestamp"].replace("Z", "+00:00"))
    window = []
    for row in reversed(normalized):
        timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        if (latest - timestamp).total_seconds() > 6 * 86400:
            break
        if timestamp.date() not in {item[0] for item in window}:
            if field in row.get("imputed_fields", []):
                return False
            value = row["calculated"].get(field)
            window.append((timestamp.date(), value))
    return len(window) >= 7 and all(value is not None and predicate(value) for _, value in window[:7])


def evaluate_rules(features: FeatureSnapshot, normalized: list[dict], horizon_days: int) -> RuleRisk:
    values, trends = features.current_values, features.trends_per_day
    findings: list[RuleFinding] = []

    def add(rule_id: str, condition: bool, severity: str, points: int, reason: str,
            value=None, threshold=None) -> None:
        if condition:
            findings.append(RuleFinding(
                rule_id=rule_id, severity=severity, points=points,
                value=value, threshold=threshold, reason=reason,
            ))

    def graded(field: str, high: float, critical: float, high_id: str, critical_id: str,
               label: str, high_reason: str, critical_reason: str) -> None:
        value = values.get(field)
        if value is None:
            return
        is_critical = value >= critical
        add(critical_id if is_critical else high_id, value >= high,
            "CRITICAL" if is_critical else "HIGH",
            RULE_POINTS["critical"] if is_critical else RULE_POINTS["high"],
            critical_reason if is_critical else high_reason, value,
            critical if is_critical else high)

    graded("co2_pct", THRESHOLDS["co2_high"], THRESHOLDS["co2_critical"],
           "ENV_CO2_001", "ENV_CO2_003", "CO₂", "Повышенный CO₂", "Критический уровень CO₂")
    add("ENV_CO2_002", trends.get("co2_pct") is not None and trends["co2_pct"] > 0.012,
        "MEDIUM", RULE_POINTS["growth"], "Наблюдается рост CO₂", trends.get("co2_pct"), 0.012)
    add("ENV_CO2_004", _persistent(normalized, "co2_pct", lambda x: x >= THRESHOLDS["co2_high"]),
        "MEDIUM", RULE_POINTS["persistent_7d"], "CO₂ превышает высокий порог не менее 7 дней", 7, 7)

    graded("water_cut_pct", THRESHOLDS["water_cut_high"], THRESHOLDS["water_cut_critical"],
           "ENV_WATER_001", "ENV_WATER_003", "water_cut", "Высокая обводнённость", "Критическая обводнённость")
    add("ENV_WATER_002", trends.get("water_cut_pct") is not None and trends["water_cut_pct"] > 0.10,
        "MEDIUM", RULE_POINTS["growth"], "Наблюдается рост обводнённости", trends.get("water_cut_pct"), 0.10)
    add("ENV_WATER_004", _persistent(normalized, "water_cut_pct", lambda x: x >= THRESHOLDS["water_cut_high"]),
        "MEDIUM", RULE_POINTS["persistent_7d"], "Высокая обводнённость сохраняется не менее 7 дней", 7, 7)

    graded("chlorides_mg_l", THRESHOLDS["chlorides_high"], THRESHOLDS["chlorides_critical"],
           "ENV_CL_001", "ENV_CL_002", "chlorides", "Высокое содержание хлоридов", "Критическое содержание хлоридов")
    add("ENV_CL_003", _persistent(normalized, "chlorides_mg_l", lambda x: x >= THRESHOLDS["chlorides_high"]),
        "MEDIUM", RULE_POINTS["persistent_7d"], "Высокое содержание хлоридов сохраняется не менее 7 дней", 7, 7)

    inhibitor = values.get("inhibitor_efficiency")
    if inhibitor is not None:
        critical = inhibitor < THRESHOLDS["inhibitor_critical"]
        low = inhibitor < THRESHOLDS["inhibitor_low"]
        add("PROT_INH_002" if critical else "PROT_INH_001", low,
            "CRITICAL" if critical else "HIGH", RULE_POINTS["critical"] if critical else RULE_POINTS["high"],
            "Критически низкая эффективность ингибиторной защиты" if critical else "Эффективность ингибиторной защиты ниже порога",
            inhibitor, THRESHOLDS["inhibitor_critical"] if critical else THRESHOLDS["inhibitor_low"])
    inhibitor_trend = trends.get("inhibitor_efficiency")
    add("PROT_INH_003", inhibitor_trend is not None and inhibitor_trend < -0.0025,
        "MEDIUM", RULE_POINTS["growth"], "Эффективность защиты устойчиво снижается", inhibitor_trend, -0.0025)
    add("PROT_INH_004", _persistent(normalized, "inhibitor_efficiency", lambda x: x < THRESHOLDS["inhibitor_low"]),
        "MEDIUM", RULE_POINTS["persistent_7d"], "Недостаточная защита сохраняется не менее 7 дней", 7, 7)

    injection = values.get("injection_deviation_pct")
    if injection is not None:
        critical = injection >= THRESHOLDS["injection_dev_critical"]
        high = injection >= THRESHOLDS["injection_dev_high"]
        add("TECH_INJ_002" if critical else "TECH_INJ_001", high,
            "CRITICAL" if critical else "HIGH", RULE_POINTS["critical"] if critical else RULE_POINTS["high"],
            "Критическое отклонение режима закачки" if critical else "Отклонение режима закачки превышает порог",
            injection, THRESHOLDS["injection_dev_critical"] if critical else THRESHOLDS["injection_dev_high"])
    add("TECH_INJ_003", _persistent(normalized, "injection_deviation_pct", lambda x: x >= THRESHOLDS["injection_dev_high"]),
        "MEDIUM", RULE_POINTS["persistent_7d"], "Отклонение режима закачки сохраняется не менее 7 дней", 7, 7)
    add("TECH_COMP_001", values.get("water_compatibility_issue") is True, "CRITICAL",
        RULE_POINTS["compatibility"], "Обнаружен признак несовместимости вод", True, None)

    rate = values.get("corrosion_rate_mm_year")
    if rate is not None:
        critical = rate >= THRESHOLDS["corrosion_rate_critical"]
        high = rate >= THRESHOLDS["corrosion_rate_high"]
        add("CORR_RATE_002" if critical else "CORR_RATE_001", high,
            "CRITICAL" if critical else "HIGH", RULE_POINTS["critical"] if critical else RULE_POINTS["high"],
            "Критическая скорость коррозии" if critical else "Высокая скорость коррозии", rate,
            THRESHOLDS["corrosion_rate_critical"] if critical else THRESHOLDS["corrosion_rate_high"])
    add("CORR_RATE_003", _persistent(normalized, "corrosion_rate_mm_year", lambda x: x >= THRESHOLDS["corrosion_rate_high"]),
        "MEDIUM", RULE_POINTS["persistent_7d"], "Высокая скорость коррозии сохраняется не менее 7 дней", 7, 7)
    wall_finding = assess_wall(values)
    if wall_finding is not None:
        critical = wall_finding["severity"] == "CRITICAL"
        add("CORR_WALL_002" if critical else "CORR_WALL_001", True, wall_finding["severity"],
            RULE_POINTS["wall_critical"] if critical else RULE_POINTS["wall_warning"],
            wall_finding["reason"], wall_finding["value"], wall_finding["threshold"])

    points = sum(item.points for item in findings)
    risk_class = "CRITICAL" if points >= 14 else "HIGH" if points >= 7 else "MEDIUM" if points >= 4 else "LOW"
    has_values = any(value is not None for value in values.values())
    return RuleRisk(
        status="ok" if has_values else "insufficient_data",
        risk_score=min(points / 14.0, 1.0) if has_values else None,
        risk_points=points if has_values else None,
        risk_class=risk_class if has_values else "UNKNOWN",
        horizon_days=horizon_days,
        findings=findings,
        note="Баллы и класс правилового движка — детерминированный экспертный индикатор, не вероятность отказа; пороги демонстрационные.",
    )


def make_action_plan(risk: RuleRisk, environment, protection, technology, corrosion) -> list[ActionItem]:
    actions: list[ActionItem] = []
    rule_ids = {item.rule_id for item in risk.findings}
    if risk.risk_class == "CRITICAL":
        actions.append(ActionItem(
            priority="CRITICAL",
            action="Организовать первоочередную инженерную оценку риска и определить необходимость внепланового обследования",
            reason="Совокупный балл Rule Engine достиг демонстрационного критического уровня.",
            owner="production_engineer/maintenance",
        ))
    if any(item.factor == "co2_pct" or item.factor == "co2_pct_trend" for item in environment.findings):
        actions.append(ActionItem(priority="HIGH", action="Провести контрольный анализ CO₂ и проверить динамику среды",
                                 reason="Повышенный или растущий CO₂.", owner="field_crew/lab"))
    if any(item.factor == "water_cut_pct" or item.factor == "water_cut_pct_trend" for item in environment.findings):
        actions.append(ActionItem(priority="MEDIUM", action="Проверить обводнённость и отобрать пробу для лабораторного анализа",
                                 reason="Повышенная или растущая обводнённость.", owner="field_crew/lab"))
    if any(item.factor == "chlorides_mg_l" for item in environment.findings):
        actions.append(ActionItem(priority="HIGH", action="Провести анализ минерализации и хлоридов",
                                 reason="Высокая концентрация хлоридов.", owner="lab"))
    if protection.findings:
        actions.append(ActionItem(priority="HIGH", action="Проверить фактическую подачу и эффективность ингибиторной защиты",
                                 reason="Недостаточная или снижающаяся защита.", owner="corrosion_service"))
    if any(item.factor == "injection_deviation_pct" for item in technology.findings):
        actions.append(ActionItem(priority="HIGH", action="Проверить режим закачки и отклонение от технологического баланса",
                                 reason="Отклонение режима закачки.", owner="production_engineer"))
    if "TECH_COMP_001" in rule_ids:
        actions.append(ActionItem(priority="HIGH", action="Провести лабораторную проверку совместимости вод",
                                 reason="Передан признак возможной несовместимости вод.", owner="lab"))
    if any(item.factor == "corrosion_rate_mm_year" for item in corrosion.findings):
        actions.append(ActionItem(priority="HIGH", action="Назначить внеплановую диагностику скорости коррозии",
                                 reason="Высокая скорость коррозии.", owner="field_crew"))
    if any(item.factor == "wall_thickness_mm" for item in corrosion.findings):
        critical = any(item.factor == "wall_thickness_mm" and item.severity == "CRITICAL" for item in corrosion.findings)
        actions.append(ActionItem(priority="CRITICAL" if critical else "HIGH",
                                 action="Проверить остаточную толщину и рассмотреть ремонтно-восстановительные мероприятия",
                                 reason="Снижение остаточной толщины стенки.", owner="maintenance"))
    if not actions:
        actions.append(ActionItem(priority="LOW", action="Продолжить плановый мониторинг",
                                 reason="Критических диагностических отклонений не выявлено.", owner="production"))
    return actions
