from __future__ import annotations

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
    ActionItem, CoxRisk, FailureHistoryItem, PipelineResult, RuleRisk, TelemetryPoint,
    WellContext, WorkHistoryItem,
)


def analyze_well(well_id: str, records: list[TelemetryPoint], horizon_days: int,
                 cox_model: CoxJsonModel | None, cox_model_error: str | None,
                 model_registry: ModelRegistry | None = None,
                 work_history: list[WorkHistoryItem] | None = None,
                 failure_history: list[FailureHistoryItem] | None = None) -> PipelineResult:
    if not records:
        raise ValueError("no telemetry records were supplied")
    dq = validate_records(records)
    normalized, normalization_report = normalize_records(records, settings.max_forward_fill_days)
    features = build_features(well_id, normalized)
    environment = diagnose_environment(features)
    protection = diagnose_protection(features)
    technology = diagnose_technology(features)
    corrosion = diagnose_corrosion(features)
    registry = model_registry or ModelRegistry.with_builtins(cox_model, cox_model_error)
    model_runs = registry.predict_all(ModelInput(
        well_id=well_id,
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
    actions = make_action_plan(rules, environment, protection, technology, corrosion)
    has_measurement = any(value is not None for value in features.current_values.values())
    confidence = max(0.25, min(1.0, 1.0 - 0.75 * features.missing_signal_fraction
                               - 0.1 * (dq.range_violations > 0)
                               - 0.1 * (dq.duplicate_timestamps > 0)))
    confidence_note = "Эвристический индикатор полноты данных; не является вероятностью правильности диагноза."
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
    if data_freshness != "fresh":
        freshness_reason = (
            f"Последнее измерение старше порога актуальности на {telemetry_age_hours:.1f} ч."
            if data_freshness == "stale"
            else "Последнее измерение имеет время в будущем; требуется сверка часов источника."
        )
        actions.insert(0, ActionItem(
            priority="HIGH",
            action="Обновить телеметрию до принятия решения по прогнозу",
            reason=freshness_reason,
            owner="production_engineer",
        ))
    context = WellContext(
        well_id=well_id, as_of=features.latest_timestamp,
        data_freshness=data_freshness, telemetry_age_hours=telemetry_age_hours,
        data_quality=dq,
        features=features, environment=environment, protection=protection,
        technology=technology, corrosion=corrosion, rule_risk=rules, cox_risk=cox,
        model_predictions=model_predictions,
        confidence=confidence, confidence_note=confidence_note,
        risk_reasons=[finding.reason for finding in rules.findings], action_plan=actions,
        work_history=work_history or [], failure_history=failure_history or [],
    )
    status = "completed_with_warnings" if (
        dq.status == "WARN" or cox.status != "ok" or data_freshness != "fresh"
        or any(item.status not in {"ok", "insufficient_data"} for item in model_predictions)
    ) else "ok"
    if not has_measurement:
        status = "insufficient_data"
    return PipelineResult(
        well_id=well_id, status=status, data_quality=dq, normalization=normalization_report,
        features=features, environment=environment, protection=protection,
        technology=technology, corrosion=corrosion, rule_risk=rules,
        cox_risk=cox, model_predictions=model_predictions, context=context,
    )
