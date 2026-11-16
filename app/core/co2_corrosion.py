"""Versioned CO₂ assessments: configured NORSOK point model and explicit legacy base."""
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


def calculate(request: CO2Calculation, model: dict | None = None) -> dict:
    inputs = request.model_dump(mode="json", exclude={"create_well"})
    if request.model_id != VERSION:
        return _norsok_assessment(request, inputs, model)
    # Preserve the original base-v4 identity across upgrades; it has no NORSOK inputs.
    for key in ('model_id', 'ph_in_situ', 'wall_shear_pa', 'water_cut_pct', 'material',
                'water_wetting', 'h2s_partial_bar', 'organic_acids_ppm'):
        inputs.pop(key)
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


def _norsok_assessment(request: CO2Calculation, inputs: dict, model: dict | None) -> dict:
    from app.core.norsok import fugacity_coefficient, load_model, point_rate
    model = model or load_model()
    if request.model_id not in {"active", model["model_id"]}:
        raise ValueError("Неизвестная CO₂-модель. Обновите список методик.")
    inputs['model_id'] = model['model_id']
    missing = [key for key in ("ph_in_situ", "wall_shear_pa") if inputs[key] is None]
    if missing:
        raise ValueError("Не хватает исходных параметров NORSOK: " + ", ".join(missing))
    if request.material != "carbon_steel":
        raise ValueError("material: для NORSOK нужно подтвердить углеродистую сталь.")
    if request.water_wetting != "water_wet":
        raise ValueError("water_wetting: NORSOK требует подтверждённого контакта воды с металлом.")
    flags = ["Скрининговая скорость общей CO₂-коррозии углеродистой стали при контакте с водой.",
             "Не учитывает другие механизмы коррозии, ингибитор и гликоль; не определяет ресурс или вероятность отказа.",
             "Использованы табличные уравнения редакции NORSOK M-506:2005; соответствие редакции 2017 не заявляется."]
    if request.h2s_partial_bar is None:
        flags.append("Содержание H₂S неизвестно; применимость по кислому газу не подтверждена.")
    elif request.h2s_partial_bar > .05:
        raise ValueError("h2s_partial_bar: выше 0,05 бар — вне принятой области MVP (ограничение редакции 2017).")
    if request.organic_acids_ppm is None:
        flags.append("Содержание органических кислот неизвестно.")
    if request.water_cut_pct is None:
        flags.append("Обводнённость не указана; контакт воды с металлом подтверждён отдельно.")
    else:
        flags.append("Обводнённость — контекст водной фазы; в NORSOK нет линейного множителя по % воды.")
    cap = model['fugacity']['pressure_cap_bar']
    if request.pressure_abs_bar > cap:
        flags.append(f"При P > {cap:g} бар коэффициент фугитивности использует ограничение {cap:g} бар; точность требует проверки.")
    if 80 <= request.temperature_c <= 100:
        flags.append("Около 90 °C возможна недостаточная консервативность модели; требуется инженерная проверка.")
    phi = fugacity_coefficient(request.temperature_c, request.pressure_abs_bar, model)
    scenarios = []
    for index, fraction in enumerate(request.co2_mole_fractions, 1):
        partial = request.pressure_abs_bar * fraction
        if partial >= request.pressure_abs_bar:
            raise ValueError('Доля CO₂: парциальное давление должно быть меньше полного (NORSOK, таблица 3).')
        if request.h2s_partial_bar and partial/request.h2s_partial_bar < 20:
            raise ValueError("h2s_partial_bar: отношение pCO₂/pH₂S ниже 20 — NORSOK неприменим.")
        if request.organic_acids_ppm is not None and request.organic_acids_ppm > 100 and partial < .5:
            raise ValueError("organic_acids_ppm: >100 ppm при pCO₂ <0,5 бар — возможное занижение; расчёт MVP заблокирован.")
        fugacity = phi * partial
        velocity = point_rate(request.temperature_c, fugacity, request.wall_shear_pa, request.ph_in_situ, model)
        scenarios.append({"scenario": index, "co2_mole_fraction": fraction, "pco2_bar": partial,
                          "fugacity_bar": fugacity, "fugacity_coefficient": phi, "rate_mm_year": velocity})
    if request.state_at is None:
        flags.append("Дата состояния среды неизвестна.")
    if request.synthetic:
        flags.append("Синтетические демонстрационные данные.")
    if request.composition_source == "scenario":
        flags.append("Доли CO₂ заданы сценариями, это не измеренный состав газа.")
    version = f"{model['model_id']}-r{model['revision']}-{model['configuration_hash'][:12]}"
    if model["rate_multiplier"] != 1:
        flags.append(f"Применена проектная калибровка скорости ×{model['rate_multiplier']:g}.")
    return {"well_id": request.well_id, "state_at": inputs["state_at"], "model_version": version,
            "model_title": model["title"], "configuration_hash": model["configuration_hash"],
            "model_snapshot": model, "calculation_mode": "calculated", "synthetic": request.synthetic,
            "input_hash": digest({"version": version, "inputs": inputs}), "inputs": inputs,
            "formula": f"Kt*fCO2^n*(S/{model['shear']['reference_pa']:g})^("
                       f"{model['shear']['exponent_constant']:g}+{model['shear']['exponent_log_fugacity']:g}*log10(fCO2))*f(pH); "
                       "node-specific n; shear omitted where configured; interpolate node rates",
            "scenarios": scenarios, "imported_results": {}, "quality_flags": flags,
            "applicability_status": "screening_water_wet_carbon_steel",
            "calculated_at": datetime.now(timezone.utc).isoformat()}
