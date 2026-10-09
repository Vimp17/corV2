from __future__ import annotations

from app.schemas import CorrosionState, DiagnosticFinding, DiagnosticResult, FeatureSnapshot


# Demonstration thresholds copied from the V3.0 notebook. Calibrate against
# field evidence before using these values for operational decisions.
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
SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3, "UNKNOWN": -1}


def assess_wall(values: dict) -> dict | None:
    """Grade wall thickness, relative to the initial wall when it is known.

    Returns None when there is no finding, else a dict with severity, value, threshold,
    basis ("relative_loss" or "absolute_thickness") and a Russian reason.
    """
    wall = values.get("wall_thickness_mm")
    if wall is None:
        return None
    initial = values.get("initial_wall_thickness_mm")
    if initial is not None and initial > 0 and initial >= wall:
        loss = (initial - wall) / initial
        for severity, key in (("CRITICAL", "wall_loss_critical"), ("HIGH", "wall_loss_warning")):
            if loss >= THRESHOLDS[key]:
                return {
                    "severity": severity, "value": round(loss, 4), "threshold": THRESHOLDS[key],
                    "basis": "relative_loss",
                    "reason": (f"Потеряно {loss:.0%} исходной толщины стенки "
                               f"({wall:g} из {initial:g} мм); порог {THRESHOLDS[key]:.0%}."),
                }
        return None
    for severity, key, text in (
        ("CRITICAL", "wall_thickness_critical", "Остаточная толщина достигла критического порога"),
        ("HIGH", "wall_thickness_warning", "Остаточная толщина ниже порога предупреждения"),
    ):
        if wall <= THRESHOLDS[key]:
            return {
                "severity": severity, "value": wall, "threshold": THRESHOLDS[key],
                "basis": "absolute_thickness",
                "reason": f"{text} ({wall:g} мм); исходная толщина неизвестна, применён абсолютный порог.",
            }
    return None


def _result(findings: list[DiagnosticFinding], has_data: bool) -> DiagnosticResult:
    if not findings and not has_data:
        return DiagnosticResult(status="insufficient_data", severity="UNKNOWN", findings=[])
    if not findings:
        return DiagnosticResult(status="ok", severity="LOW", findings=[])
    severity = max((item.severity for item in findings), key=lambda level: SEVERITY_RANK[level])
    return DiagnosticResult(status="ok", severity=severity, findings=findings)


def diagnose_environment(features: FeatureSnapshot) -> DiagnosticResult:
    values, trends = features.current_values, features.trends_per_day
    findings: list[DiagnosticFinding] = []
    rules = (
        ("co2_pct", "CO₂", "co2_high", "co2_critical", "Повышенная доля CO₂"),
        ("water_cut_pct", "Обводнённость", "water_cut_high", "water_cut_critical", "Высокая обводнённость"),
        ("chlorides_mg_l", "Хлориды", "chlorides_high", "chlorides_critical", "Высокая концентрация хлоридов"),
    )
    for field, label, high_key, critical_key, reason in rules:
        value = values.get(field)
        if value is None:
            continue
        if value >= THRESHOLDS[critical_key]:
            severity, threshold = "CRITICAL", THRESHOLDS[critical_key]
        elif value >= THRESHOLDS[high_key]:
            severity, threshold = "HIGH", THRESHOLDS[high_key]
        else:
            continue
        findings.append(DiagnosticFinding(
            factor=field, severity=severity, value=value, threshold=threshold,
            reason=f"{reason}: {value:g}.",
        ))
    for field, cutoff, label, reason in (
        ("co2_pct", 0.012, "CO₂", "Наблюдается рост CO₂"),
        ("water_cut_pct", 0.10, "Обводнённость", "Наблюдается рост обводнённости"),
    ):
        slope = trends.get(field)
        if slope is not None and slope > cutoff:
            findings.append(DiagnosticFinding(
                factor=f"{field}_trend", severity="MEDIUM", value=values.get(field),
                trend_per_day=slope, threshold=cutoff, reason=f"{reason}: {slope:.4g} в сутки.",
            ))
    return _result(findings, any(values.get(field) is not None for field, *_ in rules)
                    or any(trends.get(field) is not None for field in ("co2_pct", "water_cut_pct")))


def diagnose_protection(features: FeatureSnapshot) -> DiagnosticResult:
    value = features.current_values.get("inhibitor_efficiency")
    slope = features.trends_per_day.get("inhibitor_efficiency")
    findings: list[DiagnosticFinding] = []
    if value is not None and value < THRESHOLDS["inhibitor_critical"]:
        findings.append(DiagnosticFinding(
            factor="inhibitor_efficiency", severity="CRITICAL", value=value,
            threshold=THRESHOLDS["inhibitor_critical"],
            reason="Критически низкая эффективность ингибиторной защиты.",
        ))
    elif value is not None and value < THRESHOLDS["inhibitor_low"]:
        findings.append(DiagnosticFinding(
            factor="inhibitor_efficiency", severity="HIGH", value=value,
            threshold=THRESHOLDS["inhibitor_low"],
            reason="Эффективность ингибиторной защиты ниже демонстрационного порога.",
        ))
    if slope is not None and slope < -0.0025:
        findings.append(DiagnosticFinding(
            factor="inhibitor_efficiency_trend", severity="HIGH", value=value,
            trend_per_day=slope, threshold=-0.0025,
            reason="Эффективность защиты снижается.",
        ))
    return _result(findings, value is not None or slope is not None)


def diagnose_technology(features: FeatureSnapshot) -> DiagnosticResult:
    value = features.current_values.get("injection_deviation_pct")
    compatibility = features.current_values.get("water_compatibility_issue")
    findings: list[DiagnosticFinding] = []
    if value is not None and value >= THRESHOLDS["injection_dev_critical"]:
        findings.append(DiagnosticFinding(
            factor="injection_deviation_pct", severity="CRITICAL", value=value,
            threshold=THRESHOLDS["injection_dev_critical"],
            reason="Критическое отклонение режима закачки.",
        ))
    elif value is not None and value >= THRESHOLDS["injection_dev_high"]:
        findings.append(DiagnosticFinding(
            factor="injection_deviation_pct", severity="HIGH", value=value,
            threshold=THRESHOLDS["injection_dev_high"],
            reason="Отклонение режима закачки превышает демонстрационный порог.",
        ))
    if compatibility is True:
        findings.append(DiagnosticFinding(
            factor="water_compatibility_issue", severity="CRITICAL", value=True,
            reason="Передан признак возможной несовместимости вод.",
        ))
    return _result(findings, value is not None or compatibility is not None)


def diagnose_corrosion(features: FeatureSnapshot) -> CorrosionState:
    values = features.current_values
    rate = values.get("corrosion_rate_mm_year")
    wall = values.get("wall_thickness_mm")
    initial = values.get("initial_wall_thickness_mm")
    metal_loss = values.get("metal_loss_mm")
    findings: list[DiagnosticFinding] = []
    if rate is not None and rate >= THRESHOLDS["corrosion_rate_critical"]:
        findings.append(DiagnosticFinding(
            factor="corrosion_rate_mm_year", severity="CRITICAL", value=rate,
            threshold=THRESHOLDS["corrosion_rate_critical"], reason="Критическая скорость коррозии.",
        ))
    elif rate is not None and rate >= THRESHOLDS["corrosion_rate_high"]:
        findings.append(DiagnosticFinding(
            factor="corrosion_rate_mm_year", severity="HIGH", value=rate,
            threshold=THRESHOLDS["corrosion_rate_high"], reason="Высокая скорость коррозии.",
        ))
    wall_finding = assess_wall(values)
    if wall_finding is not None:
        findings.append(DiagnosticFinding(
            factor="wall_thickness_mm", severity=wall_finding["severity"],
            value=wall_finding["value"], threshold=wall_finding["threshold"],
            reason=wall_finding["reason"],
        ))
    if findings:
        severity = max((item.severity for item in findings), key=lambda level: SEVERITY_RANK[level])
        status = "ok"
    elif rate is not None or wall is not None:
        severity, status = "LOW", "ok"
    else:
        severity, status = "UNKNOWN", "insufficient_data"
    return CorrosionState(
        status=status, severity=severity, corrosion_rate_mm_year=rate,
        wall_thickness_mm=wall, initial_wall_thickness_mm=initial,
        metal_loss_mm=metal_loss, findings=findings,
        note="Состояние коррозии описывает физические показатели и не является вероятностью отказа.",
    )
