"""Tabulated M-506:2005 point calculation; pure equations, no DB or AI.

Equations 1–3 interpolate calculated node rates, not individual coefficients.
Tables 1–2 and configuration identity are kept alongside the model version.
This implements the point model, not the optional water-chemistry/flow solvers.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings

Number = Annotated[float, Field(allow_inf_nan=False, strict=True)]


class ModelConfigurationError(ValueError):
    """Operator configuration error, distinct from out-of-domain user input."""


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PH(_Config):
    upper: Number
    polynomial: list[Number] | None = Field(default=None, min_length=1, max_length=5)
    exponential: list[Number] | None = Field(default=None, min_length=2, max_length=2)

    @model_validator(mode="after")
    def one_function(self):
        if (self.polynomial is None) == (self.exponential is None):
            raise ValueError("Для ветви pH задайте одну функцию: polynomial или exponential.")
        return self


class _Knot(_Config):
    temperature_c: Number
    kt: Annotated[Number, Field(gt=0)]
    co2_power: Annotated[Number, Field(gt=0)]
    shear: bool
    ph: list[_PH] = Field(min_length=1, max_length=10)


class _Fugacity(_Config):
    constant: Number
    inverse_temperature: Annotated[Number, Field(gt=0)]
    pressure_cap_bar: Annotated[Number, Field(gt=0)]


class _Shear(_Config):
    reference_pa: Annotated[Number, Field(gt=0)]
    exponent_constant: Number
    exponent_log_fugacity: Number


class _Model(_Config):
    model_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    revision: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    algorithm: Literal["norsok-tabulated-v1"]
    source: str = Field(min_length=1, max_length=500)
    rate_multiplier: Annotated[Number, Field(gt=0)] = 1.0
    domain: dict[str, tuple[Number, Number]]
    fugacity: _Fugacity
    shear: _Shear
    knots: list[_Knot] = Field(min_length=2, max_length=30)

    @model_validator(mode="after")
    def coherent_tables(self):
        if len(f'{self.model_id}-r{self.revision}-' + '0'*12) > 64:
            raise ValueError('ID и revision должны помещаться с хешем в версию длиной 64 символа.')
        required = {"temperature_c", "pressure_abs_bar", "ph_in_situ", "fugacity_bar", "wall_shear_pa"}
        if set(self.domain) != required or any(lo >= hi for lo, hi in self.domain.values()):
            raise ValueError("Неверные границы области применения модели.")
        temperatures = [p.temperature_c for p in self.knots]
        if temperatures != sorted(set(temperatures)) or (temperatures[0], temperatures[-1]) != self.domain["temperature_c"]:
            raise ValueError("Температурные узлы должны однозначно покрывать область модели.")
        for point in self.knots:
            boundaries = [p.upper for p in point.ph]
            if (boundaries != sorted(set(boundaries)) or boundaries[0] <= self.domain["ph_in_situ"][0]
                    or boundaries[-1] != self.domain["ph_in_situ"][1]):
                raise ValueError("Ветви pH не покрывают область модели.")
        if self.domain["fugacity_bar"][0] <= 0 or self.domain["wall_shear_pa"][0] <= 0:
            raise ValueError("Фугитивность и сдвиг должны быть положительными.")
        if self.domain['temperature_c'][0] <= -273.15 or not 0 <= self.domain['ph_in_situ'][0] < self.domain['ph_in_situ'][1] <= 14:
            raise ValueError('Недопустимая физическая область температуры или pH.')
        return self


def load_model(path: Path | None = None) -> dict:
    """Validate configurable data without evaluating expressions or loading executable plugins."""
    path = Path(path or settings.co2_model_path)
    try:
        model = _Model.model_validate(json.loads(path.read_text(encoding="utf-8-sig"))).model_dump(exclude_none=True)
    except (OSError, ValueError) as exc:
        raise ModelConfigurationError("Конфигурация CO₂-модели не читается или содержит неверные коэффициенты.") from exc
    canonical = json.dumps(model, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    model["configuration_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
    return model


def _in_range(value, name: str, limits) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not limits[0] <= value <= limits[1]):
        raise ValueError(f"{name}: допустимый диапазон {limits[0]:g}–{limits[1]:g}.")


def _node_rate(node: dict, fugacity: float, shear: float, ph: float, flow_config: dict) -> float:
    branches = node["ph"]
    branch = next((b for b in branches if ph < b["upper"]), branches[-1])
    if "polynomial" in branch:
        factor = sum(c * ph ** power for power, c in enumerate(branch["polynomial"]))
    else:
        a, b = branch["exponential"]
        factor = a * math.exp(b * ph)
    flow = ((shear / flow_config['reference_pa']) **
            (flow_config['exponent_constant'] + flow_config['exponent_log_fugacity'] * math.log10(fugacity))
            if node["shear"] else 1)
    return node["kt"] * fugacity ** node["co2_power"] * flow * factor


def point_rate(temperature_c: float, fugacity_bar: float, wall_shear_pa: float, ph_in_situ: float, model: dict) -> float:
    for name, value in (("temperature_c", temperature_c), ("fugacity_bar", fugacity_bar),
                        ("wall_shear_pa", wall_shear_pa), ("ph_in_situ", ph_in_situ)):
        _in_range(value, name, model["domain"][name])
    points = model["knots"]
    upper = next((i for i, p in enumerate(points) if p["temperature_c"] >= temperature_c), len(points)-1)
    high = points[upper]
    try:
        value = _node_rate(high, fugacity_bar, wall_shear_pa, ph_in_situ, model['shear'])
        if high["temperature_c"] != temperature_c:
            low = points[upper-1]
            lower_value = _node_rate(low, fugacity_bar, wall_shear_pa, ph_in_situ, model['shear'])
            weight = (temperature_c-low["temperature_c"])/(high["temperature_c"]-low["temperature_c"])
            value = lower_value + weight*(value-lower_value)
        value *= model["rate_multiplier"]
    except ArithmeticError as exc:
        raise ModelConfigurationError("Переполнение при расчёте. Проверьте коэффициенты модели.") from exc
    if not math.isfinite(value) or value < 0:
        raise ModelConfigurationError("Модель вернула недопустимую скорость. Проверьте конфигурацию.")
    return value


def fugacity_coefficient(temperature_c: float, pressure_abs_bar: float, model: dict) -> float:
    _in_range(pressure_abs_bar, "pressure_abs_bar", model['domain']['pressure_abs_bar'])
    _in_range(temperature_c, "temperature_c", model["domain"]["temperature_c"])
    config = model["fugacity"]
    pressure = min(pressure_abs_bar, config["pressure_cap_bar"])
    try:
        value = 10 ** (pressure * (config["constant"] - config["inverse_temperature"]/(temperature_c+273.15)))
    except ArithmeticError as exc:
        raise ModelConfigurationError('Недопустимый коэффициент фугитивности. Проверьте конфигурацию.') from exc
    if not math.isfinite(value) or value <= 0:
        raise ModelConfigurationError('Недопустимый коэффициент фугитивности. Проверьте конфигурацию.')
    return value
