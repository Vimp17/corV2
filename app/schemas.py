from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WellRequest(APIModel):
    well_id: str = Field(min_length=1, max_length=128)

    @field_validator("well_id")
    @classmethod
    def trim_well_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("well_id cannot be blank")
        return value


class TelemetryPoint(BaseModel):
    """Canonical telemetry units follow the V3.0 notebook column names."""

    model_config = ConfigDict(extra="allow")

    timestamp: datetime
    water_cut_pct: float | None = None
    co2_pct: float | None = None
    chlorides_mg_l: float | None = None
    inhibitor_efficiency: float | None = None
    injection_deviation_pct: float | None = None
    corrosion_rate_mm_year: float | None = None
    wall_thickness_mm: float | None = None
    initial_wall_thickness_mm: float | None = None
    metal_loss_mm: float | None = None
    corrosion_load: float | None = None
    water_compatibility_issue: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def compatibility_alias(cls, value):
        if isinstance(value, dict) and "water_compatibility_issue" not in value and "incompatible_water" in value:
            value = {**value, "water_compatibility_issue": value["incompatible_water"]}
        return value

    @field_validator(
        "water_cut_pct", "co2_pct", "chlorides_mg_l", "inhibitor_efficiency",
        "injection_deviation_pct", "corrosion_rate_mm_year", "wall_thickness_mm",
        "initial_wall_thickness_mm", "metal_loss_mm", "corrosion_load",
    )
    @classmethod
    def finite_measurements(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("measurement must be a finite number")
        return value


class IngestionRequest(WellRequest):
    source_id: str = Field(min_length=1, max_length=128)
    batch_id: str | None = Field(default=None, min_length=1, max_length=128)
    records: list[TelemetryPoint] = Field(min_length=1, max_length=10000)

    @field_validator("batch_id")
    @classmethod
    def trim_batch_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("batch_id cannot be blank")
        return value

    @field_validator("source_id")
    @classmethod
    def normalize_source_id(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("source_id cannot be blank")
        return value


class WorkHistoryItem(APIModel):
    date: datetime
    work_type: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=2000)
    equipment: str | None = Field(default=None, max_length=256)
    result: str | None = Field(default=None, max_length=2000)


class FailureHistoryItem(APIModel):
    failure_date: datetime
    equipment: str | None = Field(default=None, max_length=256)
    failure_type: str = Field(min_length=1, max_length=256)
    cause: str | None = Field(default=None, max_length=2000)
    corrosion_detected: bool | None = None
    repair: str | None = Field(default=None, max_length=2000)
    downtime_days: int | None = Field(default=None, ge=0, le=36500)


class HistoryIngestionRequest(WellRequest):
    source_id: str = Field(default="ERA", min_length=1, max_length=128)
    batch_id: str | None = Field(default=None, min_length=1, max_length=128)
    work_history: list[WorkHistoryItem] = Field(default_factory=list, max_length=10000)
    failure_history: list[FailureHistoryItem] = Field(default_factory=list, max_length=10000)

    @field_validator("batch_id")
    @classmethod
    def trim_batch_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("batch_id cannot be blank")
        return value

    @field_validator("source_id")
    @classmethod
    def normalize_source_id(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("source_id cannot be blank")
        return value

    @model_validator(mode="after")
    def require_history(self):
        if not self.work_history and not self.failure_history:
            raise ValueError("at least one work or failure history record is required")
        return self


class AnalysisRequest(WellRequest):
    records: list[TelemetryPoint] | None = Field(default=None, max_length=10000)
    horizon_days: int = Field(default=90, ge=1, le=3650)
    work_history: list[WorkHistoryItem] | None = Field(default=None, max_length=10000)
    failure_history: list[FailureHistoryItem] | None = Field(default=None, max_length=10000)


class DatasetRequest(WellRequest):
    records: list[TelemetryPoint] = Field(min_length=1, max_length=10000)


class LandmarkObservation(APIModel):
    duration_remaining_days: float = Field(ge=0)
    event_observed: bool
    risk_scores: dict[str, float] = Field(default_factory=dict)
    probabilities: dict[str, float] = Field(default_factory=dict)

    @field_validator("risk_scores", "probabilities")
    @classmethod
    def finite_values(cls, values: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(value) for value in values.values()):
            raise ValueError("scores must be finite numbers")
        if any(not 0 <= value <= 1 for value in values.values()):
            raise ValueError("scores and probabilities must be in [0, 1]")
        return values


class LandmarkValidationRequest(APIModel):
    landmark_day: int = Field(ge=0)
    horizon_days: int = Field(ge=1, le=3650)
    observations: list[LandmarkObservation] = Field(min_length=1, max_length=100000)


class QualityIssue(APIModel):
    record_index: int
    code: str
    field: str | None = None
    value: Any | None = None
    message: str


class DataQualityReport(APIModel):
    status: str
    records_total: int
    records_with_issues: int
    missing_values: int
    range_violations: int
    duplicate_timestamps: int
    timezone_missing_timestamps: int
    frozen_runs: int
    issues: list[QualityIssue]


class NormalizationReport(APIModel):
    records_total: int
    records_sorted: bool
    raw_values_preserved: bool
    calculated_values_added: bool
    imputed_values: int
    forward_fill_limit_days: int
    note: str


class FeatureSnapshot(APIModel):
    well_id: str
    latest_timestamp: datetime
    observation_count: int
    days_covered: float
    missing_signal_fraction: float
    current_values: dict[str, float | bool | None]
    feature_sources: dict[str, str]
    trends_per_day: dict[str, float | None]
    trend_observation_count: dict[str, int]


class DiagnosticFinding(APIModel):
    factor: str
    severity: str
    value: float | bool | None = None
    threshold: float | None = None
    trend_per_day: float | None = None
    reason: str


class DiagnosticResult(APIModel):
    status: str
    severity: str
    findings: list[DiagnosticFinding]


class CorrosionState(APIModel):
    status: str
    severity: str
    corrosion_rate_mm_year: float | None = None
    wall_thickness_mm: float | None = None
    initial_wall_thickness_mm: float | None = None
    metal_loss_mm: float | None = None
    findings: list[DiagnosticFinding]
    note: str


class RuleFinding(APIModel):
    rule_id: str
    status: str = "triggered"
    severity: str
    points: int
    value: float | int | bool | None = None
    threshold: float | int | None = None
    reason: str


class RuleRisk(APIModel):
    model: str = "rules"
    model_version: str = "1.0"
    status: str
    risk_score: float | None = Field(default=None, description=(
        "Rule points divided by the CRITICAL threshold (14), capped at 1. A normalised index "
        "for ranking, not a probability of failure."))
    risk_points: int | None = None
    risk_class: str
    risk_percentile: float | None = None
    horizon_days: int
    is_probability: bool = False
    findings: list[RuleFinding]
    note: str


class CoxRisk(APIModel):
    model: str = "cox"
    model_version: str | None = None
    status: str
    risk_score: float | None = None
    risk_percentile: float | None = None
    horizon_days: int
    is_probability: bool = True
    calibration_status: str = "not_validated"
    reason: str | None = None


class ActionItem(APIModel):
    priority: str
    action: str
    reason: str
    owner: str


class ModelPrediction(APIModel):
    """Common, versioned output contract for pluggable risk models."""

    model_id: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    status: Literal["ok", "unavailable", "error", "insufficient_data", "unsupported_horizon"]
    horizon_days: int = Field(ge=1, le=3650)
    score: float | None = None
    score_kind: Literal["probability", "normalized_score", "risk_points", "relative_risk", "custom"] = "custom"
    probability: float | None = None
    calibrated: bool = False
    risk_class: str | None = None
    explanation: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("score", "probability")
    @classmethod
    def finite_scores(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("model scores must be finite numbers")
        return value

    @field_validator("probability")
    @classmethod
    def probability_range(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 1:
            raise ValueError("probability must be in [0, 1]")
        return value

    @model_validator(mode="after")
    def probability_semantics(self):
        if self.score_kind == "probability":
            for value in (self.score, self.probability):
                if value is not None and not 0 <= value <= 1:
                    raise ValueError("probability model scores must be in [0, 1]")
        if self.calibrated and self.probability is None:
            raise ValueError("calibrated predictions must include a probability")
        return self


class RiskModelDescriptor(APIModel):
    model_id: str
    model_version: str
    available: bool
    availability_reason: str | None = None


class RiskModelRegistryResponse(APIModel):
    models: list[RiskModelDescriptor]
    plugin_load_errors: list[str] = Field(default_factory=list)


class ExternalSourceStatus(APIModel):
    source_id: str = Field(min_length=1, max_length=128)
    status: Literal["ok", "partial", "unavailable", "not_configured", "error"]
    retrieved_at: datetime | None = None
    record_count: int | None = Field(default=None, ge=0)
    message: str | None = Field(default=None, max_length=2000)


class CrewAvailability(APIModel):
    crew_id: str = Field(min_length=1, max_length=128)
    specialties: list[str] = Field(default_factory=list, max_length=100)
    region: str | None = Field(default=None, max_length=128)
    status: Literal["available", "busy", "unknown"] = "unknown"
    available_from: datetime | None = None
    available_to: datetime | None = None
    source_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_availability_window(self):
        if (self.available_from is not None and self.available_to is not None
                and self.available_to < self.available_from):
            raise ValueError("available_to must not be earlier than available_from")
        return self


class ExternalWellData(APIModel):
    """External information collected by workflow connectors, never invented by the LLM."""

    source_statuses: list[ExternalSourceStatus] = Field(default_factory=list)
    crew_availability: list[CrewAvailability] = Field(default_factory=list, max_length=5000)
    notes: list[str] = Field(default_factory=list, max_length=500)


class RecommendedAction(APIModel):
    priority: Literal["critical", "high", "medium", "low"]
    title: str = Field(min_length=1, max_length=256)
    instruction: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=4000)
    evidence: list[str] = Field(default_factory=list, max_length=100)
    suggested_team: str | None = Field(default=None, max_length=256)
    requires_human_approval: bool = True


class WellReport(APIModel):
    well_id: str
    executive_summary: str = Field(min_length=1, max_length=10000)
    risk_interpretation: str = Field(min_length=1, max_length=10000)
    data_gaps: list[str] = Field(default_factory=list, max_length=500)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list, max_length=100)
    crew_schedule_comment: str = Field(default="Нет подтверждённых данных о расписании бригад.", max_length=4000)
    human_review_required: bool = True
    disclaimer: str = Field(default="Рекомендации сформированы ИИ по переданным данным и требуют проверки ответственным специалистом.", max_length=4000)

    @model_validator(mode="after")
    def require_human_review(self):
        if not self.human_review_required:
            raise ValueError("human_review_required must be true")
        if any(not action.requires_human_approval for action in self.recommended_actions):
            raise ValueError("every recommended action must require human approval")
        return self


class WellContext(APIModel):
    well_id: str
    as_of: datetime
    data_freshness: Literal["fresh", "stale", "future_timestamp", "unknown"] = "unknown"
    telemetry_age_hours: float | None = None
    data_origin: str = "OBSERVED_TELEMETRY"
    data_quality: DataQualityReport
    features: FeatureSnapshot
    environment: DiagnosticResult
    protection: DiagnosticResult
    technology: DiagnosticResult
    corrosion: CorrosionState
    rule_risk: RuleRisk
    cox_risk: CoxRisk
    model_predictions: list[ModelPrediction] = Field(default_factory=list)
    confidence: float
    confidence_note: str
    risk_reasons: list[str]
    action_plan: list[ActionItem]
    work_history: list[WorkHistoryItem] = Field(default_factory=list)
    failure_history: list[FailureHistoryItem] = Field(default_factory=list)
    ai_agent_status: str = "not_configured"


class AgentWellRequest(APIModel):
    analysis_id: str | None = Field(default=None, min_length=1, max_length=64)
    context: WellContext
    external_data: ExternalWellData = Field(default_factory=ExternalWellData)


class AgentWellResponse(APIModel):
    status: Literal["ok", "not_configured"]
    analysis_id: str | None = None
    provider: str | None = None
    model: str | None = None
    report: WellReport | None = None
    message: str | None = None


class SkippedAnalysis(APIModel):
    """A deliberate no-model result with actionable data/source diagnostics."""

    analysis_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    well_id: str
    status: Literal["skipped_insufficient_data"] = "skipped_insufficient_data"
    horizon_days: int | None = Field(default=None, ge=1, le=3650)
    telemetry_records: int = Field(ge=0)
    unique_timestamps: int = Field(ge=0)
    current_signal_count: int = Field(ge=0)
    missing_items: list[str] = Field(default_factory=list)
    source_statuses: list[ExternalSourceStatus] = Field(default_factory=list)
    operator_message: str
    requested_data: list[str] = Field(default_factory=list)


class MonitoringResult(APIModel):
    status: Literal["completed", "needs_configuration"]
    started_at: datetime
    completed_at: datetime
    wells_checked: int = Field(ge=0)
    analyses_completed: int = Field(ge=0)
    analyses_skipped: int = Field(ge=0)
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    source_failures: list[dict[str, Any]] = Field(default_factory=list)
    dashboard_url: str
    message: str


class MonitorRequest(APIModel):
    horizon_days: int = Field(default=90, ge=1, le=3650)


class DashboardSnapshot(APIModel):
    generated_at: datetime
    last_analysis_at: datetime | None = None
    wells_total: int
    at_risk_count: int
    data_issue_count: int
    ranking: list[dict[str, Any]] = Field(default_factory=list)


class PipelineResult(APIModel):
    analysis_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    well_id: str
    status: str
    data_quality: DataQualityReport
    normalization: NormalizationReport
    features: FeatureSnapshot
    environment: DiagnosticResult
    protection: DiagnosticResult
    technology: DiagnosticResult
    corrosion: CorrosionState
    rule_risk: RuleRisk
    cox_risk: CoxRisk
    model_predictions: list[ModelPrediction] = Field(default_factory=list)
    context: WellContext
    data_update_required: bool = False


class ValidationModelMetrics(APIModel):
    known_observations: int
    events_within_horizon: int
    roc_auc: float | None = None
    pr_auc: float | None = None
    brier_score: float | None = None
    mean_predicted_probability: float | None = None
    observed_event_rate: float | None = None
    calibration_status: str


class LandmarkValidationResult(APIModel):
    status: str
    landmark_day: int
    horizon_days: int
    records_total: int
    records_excluded_early_censoring: int
    models: dict[str, ValidationModelMetrics]
    note: str
