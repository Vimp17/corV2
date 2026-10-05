from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from app.schemas import CoxRisk, FeatureSnapshot

logger = logging.getLogger(__name__)


@dataclass
class CoxJsonModel:
    model_version: str
    features: list[str]
    medians: dict[str, float]
    means: dict[str, float]
    coefficients: dict[str, float]
    baseline_survival: list[tuple[float, float]]

    @classmethod
    def load(cls, path: Path) -> "CoxJsonModel":
        raw = json.loads(path.read_text(encoding="utf-8"))
        features = raw["features"]
        medians = raw["medians"]
        means = raw["means"]
        coefficients = raw["coefficients"]
        baseline = raw["baseline_survival"]
        if not features or any(feature not in medians or feature not in means or feature not in coefficients for feature in features):
            raise ValueError("invalid Cox artifact: features, medians, or coefficients are incomplete")
        points = sorted((float(item["time_days"]), float(item["survival"])) for item in baseline)
        if (not points
                or any(not math.isfinite(time) or not math.isfinite(value) for time, value in points)
                or any(time < 0 or not 0 <= value <= 1 for time, value in points)
                or any(points[index][0] >= points[index + 1][0] for index in range(len(points) - 1))
                or any(points[index][1] < points[index + 1][1] for index in range(len(points) - 1))):
            raise ValueError("invalid Cox artifact: baseline_survival must contain probabilities in [0, 1]")
        if any(not math.isfinite(value) for value in [*medians.values(), *means.values(), *coefficients.values()]):
            raise ValueError("invalid Cox artifact: coefficients and medians must be finite")
        return cls(
            model_version=str(raw.get("model_version", "1.0")),
            features=list(features), medians={key: float(medians[key]) for key in features},
            means={key: float(means[key]) for key in features},
            coefficients={key: float(coefficients[key]) for key in features},
            baseline_survival=points,
        )

    def predict(self, features: FeatureSnapshot, horizon_days: int) -> CoxRisk:
        values = features.current_values
        if horizon_days > self.baseline_survival[-1][0]:
            return CoxRisk(status="unsupported_horizon", model_version=self.model_version,
                           risk_score=None, horizon_days=horizon_days,
                           reason="Requested horizon exceeds the baseline model follow-up.")
        linear_predictor = 0.0
        for name in self.features:
            if name in values:
                value = values.get(name)
            elif name.endswith("_trend") and name[:-6] in features.trends_per_day:
                value = features.trends_per_day.get(name[:-6])
            else:
                return CoxRisk(status="error", model_version=self.model_version,
                               risk_score=None, horizon_days=horizon_days,
                               reason=f"Model feature is not present in the API feature contract: {name}")
            x = self.medians[name] if value is None else float(value)
            if not math.isfinite(x):
                return CoxRisk(status="error", model_version=self.model_version, horizon_days=horizon_days,
                               risk_score=None, reason=f"Non-finite feature: {name}")
            # lifelines centers covariates at their training means when fitting CoxPHFitter.
            linear_predictor += self.coefficients[name] * (x - self.means[name])
        # Last baseline-survival point at or before the requested horizon (right-continuous step curve).
        survival0 = 1.0
        for time_days, survival in self.baseline_survival:
            if time_days > horizon_days:
                break
            survival0 = survival
        hazard_ratio = math.exp(max(-20.0, min(20.0, linear_predictor)))
        risk = 1.0 - survival0 ** hazard_ratio
        return CoxRisk(
            status="ok", model_version=self.model_version,
            risk_score=max(0.0, min(1.0, risk)), horizon_days=horizon_days,
            risk_percentile=None,
        )


def load_optional_model(path: Path) -> tuple[CoxJsonModel | None, str | None]:
    if not path.exists():
        return None, "Cox model artifact is not configured; train and register a model to enable this endpoint."
    try:
        return CoxJsonModel.load(path), None
    except Exception as exc:  # Optional model errors must not disable rules/DQ endpoints.
        logger.exception("Cox model artifact could not be loaded")
        return None, f"Cox model artifact could not be loaded: {exc}"


def predict_cox(model: CoxJsonModel | None, model_error: str | None,
                features: FeatureSnapshot, horizon_days: int) -> CoxRisk:
    if model is None:
        return CoxRisk(status="unavailable" if model_error and "not configured" in model_error else "error",
                       model_version=None, risk_score=None, horizon_days=horizon_days,
                       reason=model_error or "Cox model is unavailable")
    return model.predict(features, horizon_days)
