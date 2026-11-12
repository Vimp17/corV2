"""Мат. модель (математическая модель): the probability of a tubing failure within the horizon.

The model is a JSON file (MAI_MATH_MODEL_PATH; config/math_model.json is a DEMONSTRATION model with
hand-picked coefficients, not trained on real failures): its inputs (indicator keys, «<key>_trend» for
the trend per day), their coefficients β, means and medians, and the baseline survival S₀(t). The
probability over the horizon h (MAI_RATING_HORIZON_DAYS) is 1 − S₀(h)^exp(Σ β·(x − mean)). An input
the well has no current value of takes the median of the model, and the forecast names it.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.core.features import TREND_FIELDS
from app.core.indicators import INDICATORS


class Unavailable(RuntimeError):
    """No artifact, an invalid one, or a horizon beyond the follow-up of the model."""


@dataclass(frozen=True)
class Model:
    version: str
    inputs: tuple[str, ...]
    coefficients: dict[str, float]
    means: dict[str, float]
    medians: dict[str, float]
    baseline: tuple[tuple[float, float], ...]   # (days, survival): days increasing, survival not

    def survival(self, days: float) -> float:
        """S₀ at the last baseline point not after `days` (a right-continuous step curve)."""
        value = 1.0
        for time_days, survival in self.baseline:
            if time_days > days:
                break
            value = survival
        return value


@dataclass
class Forecast:
    probability: float
    version: str
    horizon_days: int
    baseline_survival: float
    # [{"key", "name", "unit", "value", "imputed", "contribution"}]: contribution = β·(x − mean)
    inputs: list[dict]

    @property
    def imputed(self) -> list[str]:
        return [item["name"] for item in self.inputs if item["imputed"]]


def _finite(raw: dict, key: str) -> float:
    value = float(raw[key])
    if not math.isfinite(value):
        raise ValueError(f"{key}: not a finite number")
    return value


@lru_cache(maxsize=4)
def _read(path: str, modified: int) -> Model:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    inputs = tuple(raw["features"])
    for key in inputs:
        base, trend = (key[:-6], True) if key.endswith("_trend") else (key, False)
        if base not in (TREND_FIELDS if trend else INDICATORS):
            raise ValueError(f"unknown input {key}")
    baseline = tuple((float(item["time_days"]), float(item["survival"])) for item in raw["baseline_survival"])
    if not inputs or not baseline or any(not math.isfinite(days) or not math.isfinite(survival) or not 0 <= survival <= 1 or days < 0 for days, survival in baseline) or any(
            (later[0] <= earlier[0] or later[1] > earlier[1]) for earlier, later in zip(baseline, baseline[1:])):
        raise ValueError("inputs and a non-increasing baseline_survival between 0 and 1 are required")
    return Model(version=str(raw.get("model_version", "1.0")), inputs=inputs,
                    coefficients={key: _finite(raw["coefficients"], key) for key in inputs},
                    means={key: _finite(raw["means"], key) for key in inputs},
                    medians={key: _finite(raw["medians"], key) for key in inputs}, baseline=baseline)


def load() -> Model:
    path = settings.math_model_path
    try:
        return _read(str(path), path.stat().st_mtime_ns)
    except FileNotFoundError:
        raise Unavailable(f"нет файла модели {path.name} (MAI_MATH_MODEL_PATH)") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Unavailable(f"файл модели {path.name} не читается: {exc}") from None


def predict(values: dict, trends: dict) -> Forecast:
    """The failure probability over MAI_RATING_HORIZON_DAYS from the current values and trends."""
    model, horizon = load(), settings.rating_horizon_days
    if horizon > model.baseline[-1][0]:
        raise Unavailable(f"горизонт {horizon} сут. дальше наблюдений модели ({model.baseline[-1][0]:g} сут.)")
    linear, inputs = 0.0, []
    for key in model.inputs:
        trend = key.endswith("_trend")
        base = key[:-6] if trend else key
        current = (trends if trend else values).get(base)
        imputed = not isinstance(current, (int, float)) or isinstance(current, bool) or not math.isfinite(current)
        x = model.medians[key] if imputed else float(current)
        contribution = model.coefficients[key] * (x - model.means[key])
        linear += contribution
        indicator = INDICATORS[base]
        inputs.append({"key": key, "name": indicator.name + (" (тренд в сутки)" if trend else ""),
                       "unit": indicator.unit, "value": x, "imputed": imputed, "contribution": round(contribution, 4)})
    s0 = model.survival(horizon)
    probability = 1.0 - s0 ** math.exp(max(-20.0, min(20.0, linear)))
    return Forecast(round(max(0.0, min(1.0, probability)), 4), model.version, horizon, s0, inputs)
