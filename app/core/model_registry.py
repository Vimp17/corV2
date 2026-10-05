from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Protocol

from app.core.cox import CoxJsonModel, predict_cox
from app.core.rules import evaluate_rules
from app.schemas import CoxRisk, FeatureSnapshot, ModelPrediction, RuleRisk

logger = logging.getLogger(__name__)
ENTRY_POINT_GROUP = "mai_corrosion.risk_models"


@dataclass(frozen=True)
class ModelInput:
    well_id: str
    features: FeatureSnapshot
    normalized_records: list[dict[str, Any]]
    horizon_days: int
    cox_model: CoxJsonModel | None
    cox_model_error: str | None


@dataclass
class ModelRun:
    prediction: ModelPrediction
    native_result: RuleRisk | CoxRisk | None = None


class RiskModel(Protocol):
    """A plugin returns a common ModelPrediction; don't treat scores as probabilities by default."""

    model_id: str
    model_version: str

    def predict(self, model_input: ModelInput) -> ModelPrediction: ...


class RuleEngineModel:
    model_id = "rules"
    model_version = "1.0"
    available = True
    availability_reason = None

    def predict(self, model_input: ModelInput) -> ModelRun:
        result = evaluate_rules(
            model_input.features, model_input.normalized_records, model_input.horizon_days
        )
        prediction = ModelPrediction(
            model_id=self.model_id,
            model_version=result.model_version,
            status=result.status,
            horizon_days=result.horizon_days,
            score=result.risk_score,
            score_kind="normalized_score",
            calibrated=False,
            risk_class=result.risk_class,
            explanation=result.note,
            details={
                "risk_points": result.risk_points,
                "findings": [item.model_dump(mode="json") for item in result.findings],
                "is_probability": False,
            },
        )
        return ModelRun(prediction=prediction, native_result=result)


class CoxSurvivalModel:
    model_id = "cox"
    available: bool

    def __init__(self, model: CoxJsonModel | None, model_error: str | None) -> None:
        self.model = model
        self.model_error = model_error
        self.model_version = model.model_version if model else "unavailable"
        self.available = model is not None
        self.availability_reason = None if model else model_error

    def predict(self, model_input: ModelInput) -> ModelRun:
        result = predict_cox(
            self.model, self.model_error, model_input.features, model_input.horizon_days
        )
        prediction = ModelPrediction(
            model_id=self.model_id,
            model_version=result.model_version or self.model_version,
            status=result.status,
            horizon_days=result.horizon_days,
            score=result.risk_score,
            score_kind="probability",
            probability=result.risk_score if result.status == "ok" else None,
            calibrated=result.calibration_status == "validated",
            explanation=result.reason or result.calibration_status,
            details={
                "risk_percentile": result.risk_percentile,
                "calibration_status": result.calibration_status,
                "is_probability": result.is_probability,
            },
        )
        return ModelRun(prediction=prediction, native_result=result)


class ModelRegistry:
    """Runs built-in and installed risk models behind one versioned output contract.

    Third-party packages can register an entry point in the
    ``mai_corrosion.risk_models`` group. The entry point must resolve to a
    model instance or a zero-argument factory that returns one.
    """

    def __init__(self, models: list[RiskModel], plugin_load_errors: list[str] | None = None) -> None:
        self.models: list[RiskModel] = []
        self.plugin_load_errors = list(plugin_load_errors or [])
        seen: set[str] = set()
        for model in models:
            model_id = str(getattr(model, "model_id", "")).strip()
            model_version = str(getattr(model, "model_version", "")).strip()
            if not model_id or not model_version or not callable(getattr(model, "predict", None)):
                self.plugin_load_errors.append(
                    f"{type(model).__name__}: missing model_id, model_version or predict(model_input)"
                )
                continue
            if model_id in seen:
                self.plugin_load_errors.append(f"{model_id}: duplicate model_id; plugin skipped")
                continue
            seen.add(model_id)
            self.models.append(model)

    @classmethod
    def with_builtins(cls, cox_model: CoxJsonModel | None,
                      cox_model_error: str | None) -> "ModelRegistry":
        models: list[RiskModel] = [RuleEngineModel(), CoxSurvivalModel(cox_model, cox_model_error)]
        errors: list[str] = []
        try:
            points = metadata.entry_points(group=ENTRY_POINT_GROUP)
        except Exception as exc:
            logger.exception("Could not enumerate risk model plugins")
            points = []
            errors.append(f"Could not enumerate {ENTRY_POINT_GROUP} plugins ({type(exc).__name__})")

        for point in points:
            try:
                loaded = point.load()
                if isinstance(loaded, type):
                    model = loaded()
                elif callable(loaded) and not callable(getattr(loaded, "predict", None)):
                    model = loaded()
                else:
                    model = loaded
                if not callable(getattr(model, "predict", None)):
                    raise TypeError("entry point must provide predict(model_input)")
                models.append(model)
            except Exception as exc:
                logger.exception("Risk model plugin failed to load: %s", point.name)
                errors.append(f"{point.name}: plugin could not be loaded ({type(exc).__name__})")
        return cls(models=models, plugin_load_errors=errors)

    def describe(self) -> list[dict[str, Any]]:
        descriptions = []
        for model in self.models:
            descriptions.append({
                "model_id": str(model.model_id),
                "model_version": str(getattr(model, "model_version", "unknown")),
                "available": bool(getattr(model, "available", True)),
                "availability_reason": getattr(model, "availability_reason", None),
            })
        return descriptions

    def predict_all(self, model_input: ModelInput) -> list[ModelRun]:
        results: list[ModelRun] = []
        for model in self.models:
            model_id = str(model.model_id)
            version = str(getattr(model, "model_version", "unknown"))
            try:
                output = model.predict(model_input)
                if isinstance(output, ModelRun):
                    run = output
                else:
                    prediction = ModelPrediction.model_validate(output)
                    run = ModelRun(prediction=prediction)
                if run.prediction.model_id != model_id:
                    raise ValueError("prediction model_id does not match the registered model")
                if run.prediction.model_version != version:
                    raise ValueError("prediction model_version does not match the registered model")
                if run.prediction.horizon_days != model_input.horizon_days:
                    raise ValueError("prediction horizon_days does not match the request")
                results.append(run)
            except Exception as exc:
                logger.exception("Risk model prediction failed: %s", model_id)
                results.append(ModelRun(prediction=ModelPrediction(
                    model_id=model_id,
                    model_version=version,
                    status="error",
                    horizon_days=model_input.horizon_days,
                    score=None,
                    score_kind="custom",
                    calibrated=False,
                    explanation=f"Model execution failed ({type(exc).__name__}).",
                )))
        return results
