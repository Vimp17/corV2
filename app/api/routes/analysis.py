from fastapi import APIRouter, HTTPException, Request

from app import db
from app.core.cox import predict_cox
from app.core.agent import AgentServiceError, generate_well_report
from app.core.diagnostics import diagnose_corrosion, diagnose_environment, diagnose_protection, diagnose_technology
from app.core.features import build_features
from app.core.normalization import normalize_records
from app.core.orchestration import analyze_saved_well
from app.core.quality import validate_records
from app.core.rules import evaluate_rules
from app.core.validation import validate_landmark
from app.config import settings
from app.schemas import (
    AgentWellRequest, AgentWellResponse, AnalysisRequest, CorrosionState, CoxRisk,
    DataQualityReport, DatasetRequest, DiagnosticResult, FeatureSnapshot,
    LandmarkValidationRequest, LandmarkValidationResult, NormalizationReport,
    PipelineResult, RiskModelRegistryResponse, RuleRisk, SkippedAnalysis, WellContext,
)

router = APIRouter(tags=["analysis"])


def _normalized(request: DatasetRequest):
    return normalize_records(request.records, settings.max_forward_fill_days)


@router.post("/dq/validate", response_model=DataQualityReport)
def validate_data(request: DatasetRequest):
    return validate_records(request.records)


@router.post("/normalize")
def normalize_data(request: DatasetRequest) -> dict:
    records, report = _normalized(request)
    return {"well_id": request.well_id, "report": report, "records": records}


@router.post("/features", response_model=FeatureSnapshot)
def features(request: DatasetRequest):
    records, _ = _normalized(request)
    return build_features(request.well_id, records)


@router.post("/diagnostics/environment", response_model=DiagnosticResult)
def environment_diagnostics(request: DatasetRequest):
    records, _ = _normalized(request)
    return diagnose_environment(build_features(request.well_id, records))


@router.post("/diagnostics/protection", response_model=DiagnosticResult)
def protection_diagnostics(request: DatasetRequest):
    records, _ = _normalized(request)
    return diagnose_protection(build_features(request.well_id, records))


@router.post("/diagnostics/technology", response_model=DiagnosticResult)
def technology_diagnostics(request: DatasetRequest):
    records, _ = _normalized(request)
    return diagnose_technology(build_features(request.well_id, records))


@router.post("/diagnostics/corrosion", response_model=CorrosionState)
def corrosion_diagnostics(request: DatasetRequest):
    records, _ = _normalized(request)
    return diagnose_corrosion(build_features(request.well_id, records))


@router.post("/risk/rules/v1", response_model=RuleRisk)
def rule_risk(request: AnalysisRequest):
    if not request.records:
        raise HTTPException(status_code=422, detail="records are required for this endpoint")
    normalized, _ = normalize_records(request.records, settings.max_forward_fill_days)
    feature_set = build_features(request.well_id, normalized)
    return evaluate_rules(feature_set, normalized, request.horizon_days)


@router.post("/risk/cox", response_model=CoxRisk)
def cox_risk(request: AnalysisRequest, http_request: Request):
    if not request.records:
        raise HTTPException(status_code=422, detail="records are required for this endpoint")
    normalized, _ = normalize_records(request.records, settings.max_forward_fill_days)
    feature_set = build_features(request.well_id, normalized)
    return predict_cox(http_request.app.state.cox_model, http_request.app.state.cox_model_error,
                       feature_set, request.horizon_days)


@router.post("/validation/landmarks", response_model=LandmarkValidationResult)
def validation(request: LandmarkValidationRequest):
    return validate_landmark(request)


@router.get("/risk/models", response_model=RiskModelRegistryResponse)
def list_risk_models(http_request: Request):
    registry = http_request.app.state.risk_model_registry
    return {
        "models": registry.describe(),
        "plugin_load_errors": registry.plugin_load_errors,
    }


@router.post("/analysis/well", response_model=PipelineResult | SkippedAnalysis)
def analyze(request: AnalysisRequest, http_request: Request):
    return analyze_saved_well(
        request.well_id, request.horizon_days, http_request.app.state,
        getattr(http_request.state, "request_id", None),
        records_override=request.records,
        work_history_override=request.work_history,
        failure_history_override=request.failure_history,
    )


@router.get("/analysis/runs/{analysis_id}")
def get_analysis_run(analysis_id: str):
    result = db.get_analysis_run(analysis_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Analysis run was not found")
    return result


@router.post("/context/well", response_model=WellContext)
def well_context(result: PipelineResult):
    return result.context


@router.post("/agent/well", response_model=AgentWellResponse)
def create_well_report(request: AgentWellRequest):
    external_data = request.external_data.model_dump(mode="json")
    if request.analysis_id:
        prior_run = db.get_analysis_run(request.analysis_id)
        if prior_run is None or prior_run["well_id"] != request.context.well_id:
            raise HTTPException(status_code=404, detail="Analysis run was not found for this well_id")
    try:
        response = generate_well_report(request)
    except AgentServiceError as exc:
        if request.analysis_id:
            db.save_agent_result(
                request.analysis_id, request.context.well_id, "error",
                {"status": "error", "message": str(exc)}, external_data,
            )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if request.analysis_id:
        saved = db.save_agent_result(
            request.analysis_id, request.context.well_id, response.status,
            response.model_dump(mode="json"), external_data,
        )
        if not saved:
            raise HTTPException(status_code=404, detail="Analysis run was not found for this well_id")
    return response
