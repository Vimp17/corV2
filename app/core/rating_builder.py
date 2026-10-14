"""Builds each well's rating row from source data and the risk models.

Inputs, all already in the database (pulled from the source APIs when they are configured, or
uploaded by users):
  DARS   - field, operating status and the telemetry the risk models run on;
  ХИМИК  - laboratory samples (profile flag "lab_samples"), tonnage and НОО of the treatment;
  ERA    - equipment runs, demount and base-inspection acts with the corrosion finding.
The НТЦ column is the score of a risk-model plugin with model_id "ntc" when one is installed,
otherwise the rule engine's normalised score; the math column is the Cox model probability.
Every value is stored with its provenance (source, date, note) so the dashboard can show where
it came from. Operator fields (plan, Промайн document, comment) are never written here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app import db
from app.config import settings
from app.core import rating
from app.core.diagnostics import THRESHOLDS
from app.core.excel_ingestion import load_profiles
from app.core.pipeline import PreparedAnalysis, prepare_well_analysis
from app.schemas import PipelineResult, SkippedAnalysis, TelemetryPoint

logger = logging.getLogger(__name__)

NTC_MODEL_ID = "ntc"
_DEFAULT_WORK_TYPES = {
    "install": ["спуск оборудования", "спуск", "монтаж оборудования"],
    "demount": ["демонтаж", "акт демонтажа", "подъем оборудования"],
    "base_inspection": ["разбор на базе", "акт разбора", "разбор", "дефектовка на базе"],
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold().replace("ё", "е")


class _Sources:
    """Source titles, laboratory sources and ERA work-type keywords from the profiles."""

    def __init__(self) -> None:
        try:
            profiles = load_profiles()
        except Exception:  # a broken profile file must not stop the rating
            logger.exception("Source profiles could not be loaded for the rating")
            profiles = {}
        self.titles: dict[str, str] = {}
        self.lab_ids: set[str] = set()
        work_types: dict[str, list[str]] = {}
        for profile_id, profile in profiles.items():
            source_id = str(profile.get("source_id", profile_id)).upper()
            self.titles[source_id] = str(profile.get("short_title") or profile.get("title") or source_id)
            if profile.get("lab_samples"):
                self.lab_ids.add(source_id)
            for key, words in (profile.get("rating_work_types") or {}).items():
                work_types.setdefault(key, []).extend(_norm(word) for word in words)
        self.work_types = {key: work_types.get(key) or _DEFAULT_WORK_TYPES[key] for key in _DEFAULT_WORK_TYPES}

    def title(self, source_id: str | None) -> str:
        parts = [part for part in str(source_id or "").split("+") if part]
        return " + ".join(self.titles.get(part, part) for part in parts) or "источник не указан"

    def classify(self, work_type: Any) -> str | None:
        text = _norm(work_type)
        if not text:
            return None
        # Base inspection first: «разбор на базе после демонтажа» is a base inspection.
        for key in ("base_inspection", "demount", "install"):
            if any(word and word in text for word in self.work_types[key]):
                return key
        return None


def _record_sources(point: TelemetryPoint) -> str:
    return str(getattr(point, "source_id", "") or "")


def _prov(sources: _Sources, source_id: str | None, when: datetime | None, note: str | None = None) -> dict:
    return {"source_id": source_id or None, "source": sources.title(source_id) if source_id else None,
            "date": _utc(when).isoformat() if when else None, "note": note}


def _latest(records: list[TelemetryPoint], field: str) -> tuple[Any, TelemetryPoint | None]:
    for point in reversed(records):
        value = getattr(point, field, None)
        if value is not None:
            return value, point
    return None, None


def _model_scores(result: PipelineResult | None, sources: _Sources) -> tuple[dict, dict]:
    values = {"model_ntc": None, "model_math": None}
    provenance: dict[str, dict] = {}
    if result is None:
        return values, provenance
    when = result.features.latest_timestamp
    horizon = result.rule_risk.horizon_days
    plugin = next((item for item in result.model_predictions
                   if item.model_id == NTC_MODEL_ID and item.status == "ok"), None)
    plugin_score = None
    if plugin is not None:
        plugin_score = plugin.probability if plugin.probability is not None else plugin.score
        if plugin_score is not None and not 0 <= plugin_score <= 1:
            plugin_score = None
    if plugin_score is not None:
        values["model_ntc"] = round(float(plugin_score), 4)
        provenance["model_ntc"] = _prov(sources, None, when,
                                        f"Модель НТЦ (плагин {plugin.model_id} v{plugin.model_version}), "
                                        f"горизонт {horizon} сут.")
    elif result.rule_risk.status == "ok" and result.rule_risk.risk_score is not None:
        values["model_ntc"] = round(float(result.rule_risk.risk_score), 4)
        provenance["model_ntc"] = _prov(sources, None, when,
                                        f"Правила НТЦ: {result.rule_risk.risk_points} баллов из 14, "
                                        f"горизонт {horizon} сут.")
    else:
        provenance["model_ntc"] = _prov(sources, None, when, "Недостаточно данных для правил НТЦ.")
    cox = result.cox_risk
    if cox.status == "ok" and cox.risk_score is not None and 0 <= cox.risk_score <= 1:
        values["model_math"] = round(float(cox.risk_score), 4)
        provenance["model_math"] = _prov(sources, None, when,
                                         f"Модель Кокса v{cox.model_version}, вероятность отказа "
                                         f"за {cox.horizon_days} сут.")
    else:
        reason = {"unavailable": "модель Кокса не обучена или не подключена (MAI_COX_MODEL_PATH)",
                  "insufficient_data": "недостаточно данных для модели Кокса"}.get(cox.status, cox.reason or cox.status)
        provenance["model_math"] = _prov(sources, None, when,
                                         f"Мат. модель недоступна: {reason}. Индекс считается по модели НТЦ.")
    return values, provenance


def build_values(well_id: str, records: list[TelemetryPoint], events: list[dict],
                 prepared: PreparedAnalysis | None, result: PipelineResult | None,
                 now: datetime | None = None) -> tuple[dict, dict]:
    """Rating field values and their provenance for one well."""
    now = now or datetime.now(timezone.utc)
    sources = _Sources()
    records = sorted(records, key=lambda point: _utc(point.timestamp))
    values: dict[str, Any] = {}
    prov: dict[str, dict] = {}

    # Field, tonnage and НОО: the latest reported value.
    for field in ("field", "tonnage_t", "noo_days"):
        value, point = _latest(records, field)
        values[field] = value
        if point is not None:
            prov[field] = _prov(sources, _record_sources(point), point.timestamp)

    # Operating status: the latest DARS status, else fresh telemetry implies the well runs.
    value, point = _latest(records, "in_operation")
    if point is not None:
        values["in_operation"] = bool(value)
        prov["in_operation"] = _prov(sources, _record_sources(point), point.timestamp)
    elif records:
        last = records[-1]
        age_hours = (now - _utc(last.timestamp)).total_seconds() / 3600
        fresh = age_hours <= settings.max_telemetry_age_hours
        values["in_operation"] = True if fresh else None
        prov["in_operation"] = _prov(sources, _record_sources(last), last.timestamp,
                                     "Статус не передан; скважина считается в работе по свежей телеметрии."
                                     if fresh else "Статус не передан, телеметрия устарела.")
    else:
        values["in_operation"] = None

    # Sampling: a laboratory sample within the configured window.
    lab_points = [point for point in records
                  if sources.lab_ids & set(_record_sources(point).split("+"))]
    if lab_points:
        last = lab_points[-1]
        recent = now - _utc(last.timestamp) <= timedelta(days=settings.rating_sampling_days)
        values["sampling"] = recent
        prov["sampling"] = _prov(sources, _record_sources(last), last.timestamp,
                                 f"Последняя проба; окно {settings.rating_sampling_days} сут.")
    else:
        values["sampling"] = False if records else None
        if records:
            lab_titles = ", ".join(sorted(sources.title(item) for item in sources.lab_ids)) or "лаборатории"
            prov["sampling"] = _prov(sources, None, None, f"Проб {lab_titles} нет.")

    # ERA: equipment run, demount and base-inspection acts.
    work = [event for event in events if event["event_type"] == "work"]
    by_kind: dict[str, list[dict]] = {"install": [], "demount": [], "base_inspection": []}
    for event in work:
        kind = sources.classify(event["payload"].get("work_type"))
        if kind:
            by_kind[kind].append(event)
    install = next((event for event in reversed(by_kind["install"]) if event["payload"].get("equipment")), None)
    if install is None:
        install = next((event for event in reversed(events) if event["payload"].get("equipment")), None)
        note = "По последнему событию с оборудованием." if install else None
    else:
        note = install["payload"].get("work_type")
    values["equipment"] = install["payload"].get("equipment") if install else None
    if install:
        prov["equipment"] = _prov(sources, install["source_id"], install["event_date"], note)

    window_start = now - timedelta(days=settings.rating_act_days)
    for kind, act_field, corrosion_field in (("demount", "demount_act", "demount_corrosion"),
                                             ("base_inspection", "base_act", "base_corrosion")):
        recent = [event for event in by_kind[kind] if _utc(event["event_date"]) >= window_start]
        if recent:
            last = recent[-1]
            values[act_field] = True
            values[corrosion_field] = last["payload"].get("corrosion_detected")
            prov[act_field] = _prov(sources, last["source_id"], last["event_date"], last["payload"].get("work_type"))
            prov[corrosion_field] = _prov(sources, last["source_id"], last["event_date"],
                                          last["payload"].get("result")
                                          or ("Признак коррозии не указан в акте."
                                              if last["payload"].get("corrosion_detected") is None else None))
        else:
            values[act_field] = False if events else None
            values[corrosion_field] = None
            if events:
                prov[act_field] = _prov(sources, None, None,
                                        f"Актов за последние {settings.rating_act_days} сут. нет.")

    # Environment: the environment diagnosis on the latest telemetry.
    rate = None
    if prepared is not None:
        severity = prepared.environment.severity
        values["corrosive_environment"] = (True if severity in {"HIGH", "CRITICAL"}
                                           else False if severity in {"LOW", "MEDIUM"} else None)
        reasons = "; ".join(item.reason for item in prepared.environment.findings
                            if item.severity in {"HIGH", "CRITICAL"})
        when = prepared.features.latest_timestamp
        contributing = {_record_sources(point) for point in
                        (_latest(records, name)[1] for name in ("co2_pct", "water_cut_pct", "chlorides_mg_l"))
                        if point is not None}
        source_id = "+".join(sorted({part for item in contributing for part in item.split("+") if part}))
        prov["corrosive_environment"] = _prov(
            sources, source_id or None, when,
            reasons or ("Показатели CO₂, обводнённости и хлоридов ниже порогов."
                        if values["corrosive_environment"] is False else "Нет показателей среды."))
        rate = prepared.features.current_values.get("corrosion_rate_mm_year")
    else:
        values["corrosive_environment"] = None

    # Category from the corrosion evidence (approved rule).
    high_rate = rate is not None and rate >= THRESHOLDS["corrosion_rate_high"]
    environment = values["corrosive_environment"]
    evidence = [environment, values.get("demount_corrosion"), values.get("base_corrosion"), rate]
    if values.get("base_corrosion") is True or (environment is True and high_rate):
        category = "corrosive"
    elif environment is True or values.get("demount_corrosion") is True or high_rate:
        category = "medium"
    elif any(item is not None for item in evidence):
        category = "noncorrosive"
    else:
        category = None
    values["category"] = category
    if category is not None:
        parts = []
        if values.get("base_corrosion") is True:
            parts.append("коррозия по акту разбора")
        if environment is True:
            parts.append("коррозионная среда")
        if values.get("demount_corrosion") is True:
            parts.append("коррозия при демонтаже")
        if rate is not None:
            parts.append(f"скорость коррозии {rate:g} мм/год (порог {THRESHOLDS['corrosion_rate_high']:g})")
        prov["category"] = _prov(sources, None, None, "; ".join(parts) or "признаков коррозии нет")

    scores, score_prov = _model_scores(result, sources)
    values.update(scores)
    prov.update(score_prov)
    return values, prov


def update_from_analysis(well_id: str, records: list[TelemetryPoint],
                         prepared: PreparedAnalysis | None,
                         results: Iterable[PipelineResult | SkippedAnalysis]) -> dict | None:
    """Recompute and store the well's rating after an analysis. Errors are logged, not raised."""
    try:
        results = list(results)
        def horizon(item) -> int | None:
            return item.rule_risk.horizon_days if isinstance(item, PipelineResult) else item.horizon_days

        chosen = next((item for item in results if horizon(item) == settings.rating_horizon_days),
                      results[0] if results else None)
        skipped = chosen if isinstance(chosen, SkippedAnalysis) else None
        result = chosen if isinstance(chosen, PipelineResult) else None
        if prepared is None and records:
            try:
                prepared = prepare_well_analysis(well_id, records)
            except Exception:  # evidence is optional for a skipped well
                logger.info("Rating evidence could not be prepared for well_id=%s", well_id, exc_info=True)
        events = db.load_history_events(well_id)
        values, provenance = build_values(well_id, records, events, prepared, result)
        measure = rating.measure_for(rating.priority_index(values["model_ntc"], values["model_math"]))
        return db.save_computed_rating(
            well_id, values, measure=measure, provenance=provenance,
            skip_reason=skipped.operator_message[:2000] if skipped else None,
            analysis_id=chosen.analysis_id if chosen is not None else None,
        )
    except Exception:
        logger.exception("Rating computation failed for well_id=%s", well_id)
        return None


def recompute_wells(well_ids: Iterable[str], app_state, request_id: str | None = None) -> dict:
    """Analyse wells on the rating horizon from stored data and refresh their rating.

    Used after uploads and pushes; no source APIs are called (the daily cycle and the per-well
    button refresh sources first).
    """
    from app.core.orchestration import analyze_saved_well_horizons

    done, failed = [], []
    for well_id in dict.fromkeys(well_ids):
        try:
            analyze_saved_well_horizons(well_id, [settings.rating_horizon_days], app_state, request_id)
            done.append(well_id)
        except Exception:
            logger.exception("Rating recompute failed for well_id=%s", well_id)
            failed.append(well_id)
    return {"recomputed": done, "failed": failed}


def schedule_recompute(well_ids: Iterable[str], app_state, background_tasks, request_id: str | None = None) -> dict:
    """Recompute now for small uploads, in the background for large ones."""
    wells = [well for well in dict.fromkeys(well_ids) if well]
    if not wells:
        return {"mode": "none", "wells": 0}
    if len(wells) <= settings.rating_recompute_sync_limit or background_tasks is None:
        outcome = recompute_wells(wells, app_state, request_id)
        return {"mode": "sync", "wells": len(wells), "failed": outcome["failed"]}
    background_tasks.add_task(recompute_wells, wells, app_state, request_id)
    return {"mode": "background", "wells": len(wells)}
