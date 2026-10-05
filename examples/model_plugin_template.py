"""Copy this template into a separately packaged model plugin and implement inference."""

from app.core.model_registry import ModelInput
from app.schemas import ModelPrediction


class ExampleSurvivalModel:
    model_id = "replace-with-stable-model-id"
    model_version = "0.1.0"
    available = False
    availability_reason = "Replace this template with a trained model artifact."

    def predict(self, model_input: ModelInput) -> ModelPrediction:
        # Keep the model unavailable until a real artifact and inference path are configured.
        # Never fill in a demonstration score and present it as a forecast.
        return ModelPrediction(
            model_id=self.model_id,
            model_version=self.model_version,
            status="unavailable",
            horizon_days=model_input.horizon_days,
            score=None,
            score_kind="custom",
            calibrated=False,
            explanation=self.availability_reason,
        )
