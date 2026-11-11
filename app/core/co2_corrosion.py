"""Reproducible v4 base CO₂ rate; scenario calculation, not a failure probability."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

from app.schemas import CO2Calculation

VERSION = "co2-v4-base-1"
FORMULA = "10^(5.8 - 1710/(T_C + 273.15) + 0.67*log10(pCO2_bar))"
LIMITATIONS = [
    "Сценарный расчёт общей CO₂-коррозии; полевая точность и область применимости не подтверждены.",
    "Скорость в мм/год не является вероятностью отказа или остаточным ресурсом.",
    "Базовая формула не учитывает ингибирование, локальные дефекты и защитную плёнку.",
]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def rate(temperature_c: float, pco2_bar: float) -> float:
    if not math.isfinite(temperature_c) or temperature_c <= -273.15:
        raise ValueError("Температура должна быть конечной и выше −273,15 °C.")
    if not math.isfinite(pco2_bar) or pco2_bar < 0:
        raise ValueError("Парциальное давление CO₂ должно быть конечным и неотрицательным.")
    return 0.0 if pco2_bar == 0 else 10 ** (5.8 - 1710 / (temperature_c + 273.15) + .67 * math.log10(pco2_bar))


def calculate(request: CO2Calculation) -> dict:
    inputs = request.model_dump(mode="json", exclude={"create_well"})
    scenarios = []
    fractions = request.co2_mole_fractions
    for index, fraction in enumerate(fractions):
        pco2 = request.pressure_abs_bar * fraction
        scenarios.append({"scenario": index + 1, "co2_mole_fraction": fraction,
                          "pco2_bar": pco2, "rate_mm_year": rate(request.temperature_c, pco2)})
    flags = list(LIMITATIONS)
    if request.state_at is None:
        flags.append("Дата состояния среды неизвестна.")
    if request.synthetic:
        flags.append("Синтетические демонстрационные данные.")
    if request.composition_source == "scenario":
        flags.append("Доли CO₂ заданы сценариями, это не измеренный состав газа.")
    return {"well_id": request.well_id, "state_at": inputs["state_at"], "model_version": VERSION,
            "calculation_mode": "calculated", "synthetic": request.synthetic,
            "input_hash": digest({"version": VERSION, "inputs": inputs}), "inputs": inputs,
            "formula": FORMULA, "scenarios": scenarios, "imported_results": {},
            "quality_flags": flags, "applicability_status": "unvalidated",
            "calculated_at": datetime.now(timezone.utc).isoformat()}
