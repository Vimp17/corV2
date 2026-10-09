from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import settings
from app.core.cox import CoxJsonModel, predict_cox
from app.core.diagnostics import (
    diagnose_corrosion, diagnose_environment, diagnose_protection, diagnose_technology,
)
from app.core.features import build_features
from app.core.normalization import normalize_records
from app.core.quality import validate_records
from app.core.model_registry import ModelInput, ModelRegistry
from app.core.rules import evaluate_rules, make_action_plan
from app.schemas import (
    ActionItem, CorrosionState, CoxRisk, DataQualityReport, DiagnosticResult, FailureHistoryItem,
    FeatureSnapshot, NormalizationReport, PipelineResult, RuleRisk, TelemetryPoint, WellContext,
    WorkHistoryItem,
)


# Cox "unavailable" means no artifact is configured yet: an expected deployment state, not a
# problem with this well's analysis, so it does not downgrade the result to a warning.
_QUIET_MODEL_STATUSES = {"ok", "insufficient_data", "unavailable"}


@dataclass
class PreparedAnalysis:
    """Horizon-independent part of a well analysis, computed once per well and data snapshot."""

    well_id: str
    dq: DataQualityReport
    normalized: list[dict]
    normalization_report: NormalizationReport
    features: FeatureSnapshot
    environment: DiagnosticResult
    protection: DiagnosticResult
    technology: DiagnosticResult
    corrosion: CorrosionState
    data_freshness: str
    telemetry_age_hours: float
    confidence: float


def prepare_well_analysis(well_id: str, records: list[TelemetryPoint]) -> PreparedAnalysis:
    if not records:
        raise ValueError("no telemetry records were supplied")
    dq = validate_records(records)
    normalized, normalization_report = normalize_records(records, settings.max_forward_fill_days)
    features = build_features(well_id, normalized)
    latest_timestamp = features.latest_timestamp
    latest_utc = (latest_timestamp.replace(tzinfo=timezone.utc) if latest_timestamp.tzinfo is None
                  else latest_timestamp.astimezone(timezone.utc))
    telemetry_age_hours = (datetime.now(timezone.utc) - latest_utc).total_seconds() / 3600
    if telemetry_age_hours < -1:
        data_freshness = "future_timestamp"
    elif telemetry_age_hours > settings.max_telemetry_age_hours:
        data_freshness = "stale"
    else:
        data_freshness = "fresh"
    confidence = max(0.25, min(1.0, 1.0 - 0.75 * features.missing_signal_fraction
                               - 0.1 * (dq.range_violations > 0)
                               - 0.1 * (dq.duplicate_timestamps > 0)))
    return PreparedAnalysis(
        well_id=well_id, dq=dq, normalized=normalized, normalization_report=normalization_report,
        features=features, environment=diagnose_environment(features),
        protection=diagnose_protection(features), technology=diagnose_technology(features),
        corrosion=diagnose_corrosion(features), data_freshness=data_freshness,
        telemetry_age_hours=telemetry_age_hours, confidence=confidence,
    )


def analyze_prepared(prepared: PreparedAnalysis, horizon_days: int,
                     cox_model: CoxJsonModel | None, cox_model_error: str | None,
                     model_registry: ModelRegistry | None = None,
                     work_history: list[WorkHistoryItem] | None = None,
                     failure_history: list[FailureHistoryItem] | None = None) -> PipelineResult:
    """Run the horizon-dependent models on a prepared analysis and assemble the result."""
    features, normalized = prepared.features, prepared.normalized
    registry = model_registry or ModelRegistry.with_builtins(cox_model, cox_model_error)
    model_runs = registry.predict_all(ModelInput(
        well_id=prepared.well_id,
        features=features,
        normalized_records=normalized,
        horizon_days=horizon_days,
        cox_model=cox_model,
        cox_model_error=cox_model_error,
    ))
    model_predictions = [run.prediction for run in model_runs]
    rules = next((run.native_result for run in model_runs if isinstance(run.native_result, RuleRisk)), None)
    if rules is None:
        # Preserve the existing required Rule Engine output if its registry adapter is malformed.
        rules = evaluate_rules(features, normalized, horizon_days)
    cox = next((run.native_result for run in model_runs if isinstance(run.native_result, CoxRisk)), None)
    if cox is None:
        cox = predict_cox(cox_model, cox_model_error, features, horizon_days)
    actions = make_action_plan(rules, prepared.environment, prepared.protection,
                               prepared.technology, prepared.corrosion)
    has_measurement = any(value is not None for value in features.current_values.values())
    confidence_note = "Эвристический индикатор полноты данных; не является вероятностью правильности диагноза."
    data_freshness, telemetry_age_hours = prepared.data_freshness, prepared.telemetry_age_hours
    if data_freshness != "fresh":
        freshness_reason = (
            f"Последнее измерение старше порога актуальности: {telemetry_age_hours:.1f} ч."
            if data_freshness == "stale"
            else "Последнее измерение имеет время в будущем; требуется сверка часов источника."
        )
        actions.insert(0, ActionItem(
            priority="HIGH",
            action="Обновить телеметрию до принятия решения по прогнозу",
            reason=freshness_reason,
            owner="production_engineer",
        ))
    dq = prepared.dq
    context = WellContext(
        well_id=prepared.well_id, as_of=features.latest_timestamp,
        data_freshness=data_freshness, telemetry_age_hours=telemetry_age_hours,
        data_quality=dq,
        features=features, environment=prepared.environment, protection=prepared.protection,
        technology=prepared.technology, corrosion=prepared.corrosion, rule_risk=rules, cox_risk=cox,
        model_predictions=model_predictions,
        confidence=prepared.confidence, confidence_note=confidence_note,
        risk_reasons=[finding.reason for finding in rules.findings], action_plan=actions,
        work_history=work_history or [], failure_history=failure_history or [],
    )
    status = "completed_with_warnings" if (
        dq.status == "WARN" or data_freshness != "fresh"
        or cox.status not in _QUIET_MODEL_STATUSES
        or any(item.status not in _QUIET_MODEL_STATUSES for item in model_predictions)
    ) else "ok"
    if not has_measurement:
        status = "insufficient_data"
    return PipelineResult(
        well_id=prepared.well_id, status=status, data_quality=dq,
        normalization=prepared.normalization_report,
        features=features, environment=prepared.environment, protection=prepared.protection,
        technology=prepared.technology, corrosion=prepared.corrosion, rule_risk=rules,
        cox_risk=cox, model_predictions=model_predictions, context=context,
        data_update_required=data_freshness != "fresh",
    )


def analyze_well(well_id: str, records: list[TelemetryPoint], horizon_days: int,
                 cox_model: CoxJsonModel | None, cox_model_error: str | None,
                 model_registry: ModelRegistry | None = None,
                 work_history: list[WorkHistoryItem] | None = None,
                 failure_history: list[FailureHistoryItem] | None = None) -> PipelineResult:
    return analyze_prepared(prepare_well_analysis(well_id, records), horizon_days,
                            cox_model, cox_model_error, model_registry,
                            work_history, failure_history)
