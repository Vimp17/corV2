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
from zoneinfo import ZoneInfo

from app import db
from app.config import settings
from app.core import rating
from app.core.diagnostics import THRESHOLDS
from app.core.excel_ingestion import load_profiles
from app.core.pipeline import PreparedAnalysis, prepare_well_analysis
from app.schemas import PipelineResult, SkippedAnalysis, TelemetryPoint

logger = logging.getLogger(__name__)

NTC_MODEL_ID = "ntc"
# Words in the equipment description that mean a protective design (coated or corrosion-
# resistant tubing, inhibitor containers). Profiles may override them ("protection_keywords").
_DEFAULT_PROTECTION_WORDS = ["покрыт", "коррозионностойк", "нержав", "стеклопластик", "футеров",
                             "протектор", "контейнер ингибитора", "ингибиторный контейнер"]
_DEFAULT_WORK_TYPES = {
    "install": ["спуск оборудования", "спуск", "монтаж оборудования"],
    "demount": ["демонтаж", "акт демонтажа", "подъем оборудования"],
    "base_inspection": ["разбор на базе", "акт разбора", "разбор", "дефектовка на базе"],
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _local(value: datetime) -> datetime:
    """A moment in the source time zone (MAI_SOURCE_TIMEZONE): a date-only value such as a
    sample taken on 16.10 is shown as 16.10, not as the evening of 15.10 in UTC."""
    return _utc(value).astimezone(ZoneInfo(settings.source_timezone))


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
        protection_words: list[str] = []
        for profile_id, profile in profiles.items():
            source_id = str(profile.get("source_id", profile_id)).upper()
            self.titles[source_id] = str(profile.get("short_title") or profile.get("title") or source_id)
            if profile.get("lab_samples"):
                self.lab_ids.add(source_id)
            for key, words in (profile.get("rating_work_types") or {}).items():
                work_types.setdefault(key, []).extend(_norm(word) for word in words)
            protection_words.extend(_norm(word) for word in profile.get("protection_keywords") or [])
        # Data taken from documents (acts, lab protocols, passports) is stored under the
        # document type's source_id; lab protocols count as laboratory samples.
        from app.core.documents.types import lab_source_ids, source_titles

        self.titles.update(source_titles())
        self.lab_ids |= lab_source_ids()
        self.protection_words = protection_words or [_norm(word) for word in _DEFAULT_PROTECTION_WORDS]
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

    def protective_equipment(self, equipment: Any) -> bool:
        text = _norm(equipment)
        return bool(text) and any(word in text for word in self.protection_words)


def _record_sources(point: TelemetryPoint) -> str:
    return str(getattr(point, "source_id", "") or "")


def _prov(sources: _Sources, source_id: str | None, when: datetime | None, note: str | None = None) -> dict:
    return {"source_id": source_id or None, "source": sources.title(source_id) if source_id else None,
            "date": _local(when).isoformat() if when else None, "note": note}


def _latest(records: list[TelemetryPoint], field: str) -> tuple[Any, TelemetryPoint | None]:
    for point in reversed(records):
        value = getattr(point, field, None)
        if value is not None:
            return value, point
    return None, None


ENV_RULE_PREFIX = "ENV_"


def _model_scores(result: PipelineResult | None, sources: _Sources,
                  protection: str | None = None) -> tuple[dict, dict]:
    """НТЦ and math scores. With confirmed effective protection (and MAI_RATING_PROTECTION_MODE
    = adjust) the environment rule points of the rule-based НТЦ score count with
    MAI_RATING_PROTECTION_FACTOR; points for measured damage are never reduced."""
    values = {"model_ntc": None, "model_math": None, "model_ntc_raw": None}
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
        if protection == "effective":
            provenance["model_ntc"]["note"] += " Защита учитывается логикой модели НТЦ, корректировки нет."
    elif result.rule_risk.status == "ok" and result.rule_risk.risk_score is not None:
        raw = round(float(result.rule_risk.risk_score), 4)
        points = result.rule_risk.risk_points or 0
        env_points = sum(item.points for item in result.rule_risk.findings
                         if item.rule_id.startswith(ENV_RULE_PREFIX))
        values["model_ntc"] = values["model_ntc_raw"] = raw
        note = f"Правила НТЦ: {points} баллов из 14, горизонт {horizon} сут."
        if protection == "effective" and env_points:
            factor = settings.rating_protection_factor
            adjusted_points = points - env_points * (1 - factor)
            if settings.rating_protection_mode == "adjust":
                values["model_ntc"] = round(min(adjusted_points / 14.0, 1.0), 4)
                note += (f" Защита эффективна: баллы среды ({env_points}) учтены с коэффициентом "
                         f"{factor:g}, итого {adjusted_points:g} баллов.")
            else:
                note += (f" Защита эффективна; с её учётом было бы {adjusted_points:g} баллов "
                         "(режим MAI_RATING_PROTECTION_MODE=info, индекс не корректируется).")
        provenance["model_ntc"] = _prov(sources, None, when, note)
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


GAP_LABELS = rating.GAP_LABELS


def extra_measurements(well_id: str) -> list[dict]:
    """Latest value of every additional indicator (pH, hardness…) with its source and date.

    These indicators have no system field: they are shown in the well card and given to the AI
    agent, but do not enter the risk models or the rating."""
    sources = _Sources()
    latest: dict[str, dict] = {}
    for row in db.load_extra_measurements(well_id):
        for name, value in (row["extras"] or {}).items():
            if isinstance(value, (int, float)):
                latest[name] = {"name": name, "value": value, "date": _local(row["timestamp"]).isoformat(),
                                "source_id": row["source_id"], "source": sources.title(row["source_id"])}
    return sorted(latest.values(), key=lambda item: item["name"].casefold())


def well_passports(well_id: str) -> list[tuple[str, dict, datetime | None]]:
    """Passport fields taken from documents: (source_id, fields, passport date)."""
    from app.core.documents.types import passport_source_ids

    ids = passport_source_ids()
    result = []
    for source_id, payload in db.load_source_payloads(well_id).items():
        if source_id in ids and isinstance(payload.get("passport"), dict):
            fields = payload["passport"]
            when = None
            if fields.get("passport_date"):
                try:
                    when = datetime.fromisoformat(fields["passport_date"]).replace(tzinfo=timezone.utc)
                except ValueError:
                    when = None
            result.append((source_id, fields, when))
    return result


def build_values(well_id: str, records: list[TelemetryPoint], events: list[dict],
                 prepared: PreparedAnalysis | None, result: PipelineResult | None,
                 now: datetime | None = None, skipped: SkippedAnalysis | None = None,
                 passports: list[tuple[str, dict, datetime | None]] | None = None) -> tuple[dict, dict]:
    """Rating field values and their provenance for one well."""
    now = now or datetime.now(timezone.utc)
    sources = _Sources()
    records = sorted(records, key=lambda point: _utc(point.timestamp))
    values: dict[str, Any] = {}
    prov: dict[str, dict] = {}

    # Field, tonnage, НОО and liquid rate: the latest reported value.
    for field in ("field", "tonnage_t", "noo_days", "liquid_rate_m3_day"):
        value, point = _latest(records, field)
        values[field] = value
        if point is not None:
            prov[field] = _prov(sources, _record_sources(point), point.timestamp)
    # A field missing from telemetry is taken from the well passport document.
    for source_id, fields, when in passports or []:
        if values.get("field") is None and fields.get("field"):
            values["field"] = str(fields["field"])[:256]
            prov["field"] = _prov(sources, source_id, when, "Из паспорта скважины.")

    # Oil share from the latest water cut (by volume).
    water_cut, point = _latest(records, "water_cut_pct")
    values["oil_pct"] = round(max(0.0, min(100.0, 100.0 - water_cut)), 1) if water_cut is not None else None
    if point is not None:
        prov["oil_pct"] = _prov(sources, _record_sources(point), point.timestamp,
                                f"100 % − обводнённость {water_cut:g} %.")

    # Operating status: the latest DARS status, else a fresh measurement implies the well runs.
    # A registry row (snapshot) is not a measurement and says nothing about the status.
    from app.core.source_kinds import is_lab_only, is_snapshot, snapshot_source_ids

    snapshot_ids = snapshot_source_ids()
    measured = [point for point in records if not is_snapshot(point, snapshot_ids)]
    # «Телеметрия (ДАРС)» in the data gaps: telemetry series only, not laboratory samples.
    series = [point for point in measured if not is_lab_only(point, sources.lab_ids)]
    value, point = _latest(records, "in_operation")
    if point is not None:
        values["in_operation"] = bool(value)
        prov["in_operation"] = _prov(sources, _record_sources(point), point.timestamp)
    elif measured:
        last = measured[-1]
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
    # Run time counts from the latest equipment run (ЭРА «Спуск оборудования»).
    last_run = by_kind["install"][-1] if by_kind["install"] else None
    values["installed_at"] = _utc(last_run["event_date"]) if last_run else None
    if last_run:
        prov["run_days"] = _prov(sources, last_run["source_id"], last_run["event_date"],
                                 "Наработка считается от даты последнего спуска оборудования.")

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

    protection, protection_note = _assess_protection(values, prepared, rate, sources)
    values["protection"] = protection
    current = prepared.features.current_values if prepared is not None else {}
    means = (["ингибитор"] if current.get("inhibitor_efficiency") is not None else []) + (
        ["защитное исполнение НКТ"] if sources.protective_equipment(values.get("equipment")) else [])
    if protection_note or means:
        when = prepared.features.latest_timestamp if prepared is not None else None
        prov["protection"] = {**_prov(sources, None, when, protection_note), "means": means}

    scores, score_prov = _model_scores(result, sources, protection)
    values.update(scores)
    prov.update(score_prov)

    # What is missing, by section, so the dashboard can say what to request: the notes name the
    # missing fields and dates, not only the system they come from.
    gaps: list[dict] = []
    if not series:
        gaps.append({"key": "telemetry", "note": "Нет замеров телеметрии: обводнённость, CO₂, хлориды, "
                                                 "дебит жидкости, статус работы скважины."})
    elif skipped is not None:
        # Readiness explains what is short; the lines about unconfigured source APIs are left out.
        reasons = [item.strip() for item in skipped.missing_items if not item.startswith("Источник ")]
        gaps.append({"key": "telemetry", "note": "Мало замеров для расчёта моделей. "
                     + (" ".join(reasons[:2]) if reasons else f"Нужно не менее {settings.min_telemetry_records} "
                        "замеров с разными датами.")})
    elif (now - _utc(series[-1].timestamp)).total_seconds() / 3600 > settings.max_telemetry_age_hours:
        gaps.append({"key": "telemetry", "note": f"Последний замер телеметрии — {_local(series[-1].timestamp):%d.%m.%Y}, "
                                                 f"старше {settings.max_telemetry_age_hours:g} ч."})
    if values.get("sampling") is not True:
        last_sample = (f"последняя — {_local(lab_points[-1].timestamp):%d.%m.%Y}" if lab_points
                       else "проб нет")
        gaps.append({"key": "samples", "note": f"Нет лабораторной пробы (CO₂, хлориды, эффективность ингибитора) "
                                               f"за {settings.rating_sampling_days} сут.: {last_sample}."})
    demounts = [event for event in by_kind["demount"] if _utc(event["event_date"]) >= window_start]
    if demounts:
        last_demount = _utc(demounts[-1]["event_date"])
        if not any(_utc(event["event_date"]) >= last_demount for event in by_kind["base_inspection"]):
            gaps.append({"key": "base_act", "note": f"Демонтаж {_local(last_demount):%d.%m.%Y} был, "
                                                    "акта разбора на базе после него нет."})
    values["data_gaps"] = gaps
    return values, prov


def _assess_protection(values: dict, prepared: PreparedAnalysis | None, rate: float | None,
                       sources: _Sources) -> tuple[str | None, str | None]:
    """Protection status: effective / unconfirmed / insufficient, or None without data.

    Measures: inhibitor efficiency at or above MAI_RATING_PROTECTION_MIN_INHIBITOR with the
    injection deviation below MAI_RATING_PROTECTION_MAX_INJECTION_DEV, or a protective design
    in the equipment description. Confirmation: a measured corrosion rate below
    MAI_RATING_PROTECTION_MAX_RATE and no corrosion in the latest demount or base act.
    Insufficient: a measure fails, corrosion is found in the latest act, or the corrosion rate
    reaches the high threshold despite the measures; a rate between the two is unconfirmed.
    """
    current = prepared.features.current_values if prepared is not None else {}
    inhibitor = current.get("inhibitor_efficiency")
    deviation = current.get("injection_deviation_pct")
    design = sources.protective_equipment(values.get("equipment"))
    min_inhibitor = settings.rating_protection_min_inhibitor
    max_deviation = settings.rating_protection_max_injection_dev
    max_rate = settings.rating_protection_max_rate

    measures, problems = [], []
    if design:
        measures.append(f"защитное исполнение: {values.get('equipment')}")
    if inhibitor is not None:
        if inhibitor >= min_inhibitor and (deviation is None or deviation < max_deviation):
            measures.append(f"ингибитор {inhibitor:.2f}"
                            + (f", отклонение закачки {deviation:.0f} %" if deviation is not None else ""))
        elif inhibitor < min_inhibitor:
            problems.append(f"эффективность ингибитора {inhibitor:.2f} ниже {min_inhibitor:g}")
        else:
            problems.append(f"отклонение закачки {deviation:.0f} % (порог {max_deviation:g} %)")
    if not measures and not problems:
        return None, None
    if values.get("base_corrosion") is True or values.get("demount_corrosion") is True:
        problems.append("коррозия в последнем акте демонтажа или разбора")
    high_rate = THRESHOLDS["corrosion_rate_high"]
    if rate is not None and rate >= high_rate and measures:
        problems.append(f"скорость коррозии {rate:.2f} мм/год при мерах защиты (порог {high_rate:g})")
    if problems:
        return "insufficient", "Защита недостаточна: " + "; ".join(problems) + (
            ". Меры: " + "; ".join(measures) + "." if measures else ".")
    if rate is None:
        return "unconfirmed", ("Защита заявлена (" + "; ".join(measures) + "), но не подтверждена: "
                               "нет замера скорости коррозии. Подтвердите эффективность.")
    if rate >= max_rate:
        return "unconfirmed", ("Защита заявлена (" + "; ".join(measures) + f"), но скорость коррозии "
                               f"{rate:.2f} мм/год не ниже порога подтверждения {max_rate:g}.")
    return "effective", ("Защита эффективна: " + "; ".join(measures)
                         + f"; скорость коррозии {rate:.2f} мм/год ниже {max_rate:g}.")


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
        values, provenance = build_values(well_id, records, events, prepared, result, skipped=skipped,
                                          passports=well_passports(well_id))
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
