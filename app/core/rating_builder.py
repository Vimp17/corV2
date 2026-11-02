"""Builds each well's rating row from its data in the database.

Inputs (pulled from the source APIs when they are configured, uploaded by users or read from
documents):
  DARS   - field, operating status and the telemetry the НТЦ rules run on;
  ХИМИК  - laboratory samples (profile flag "lab_samples"), tonnage and НОО of the treatment;
  ERA    - equipment runs, demount and base-inspection acts with the corrosion finding.
The НТЦ column is the rule score (app.core.rules) on the current values and trends, computed when
the well has enough measurements (app.core.readiness). Every value is stored with its provenance
(source, date, note) so the dashboard can show where it came from. Operator fields (plan, Промайн
document, comment) are never written here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app import db
from app.config import settings
from app.core import rating, readiness
from app.core.excel_ingestion import load_profiles
from app.core.features import Features, build_features
from app.core.normalization import normalize_records
from app.core.rules import ENVIRONMENT_PREFIX, THRESHOLDS, Finding, evaluate_rules, score
from app.core.source_kinds import lab_source_ids, snapshot_source_ids
from app.schemas import TelemetryPoint

logger = logging.getLogger(__name__)
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


@dataclass
class Prepared:
    """Current values and trends of a well and their normalised rows (the input of the rules)."""

    features: Features
    normalized: list[dict]


def prepare(records: list[TelemetryPoint]) -> Prepared:
    """Laboratory samples and registry snapshots stay current for the sampling window, telemetry for
    MAI_MAX_FORWARD_FILL_DAYS (app.core.source_kinds)."""
    snapshots = snapshot_source_ids()
    normalized = normalize_records(records, settings.max_forward_fill_days,
                                   sample_fill_days=settings.rating_sampling_days,
                                   sample_sources=lab_source_ids() | snapshots, snapshot_sources=snapshots)
    return Prepared(build_features(normalized), normalized)


# Corrosive environment: CO₂, water cut or chlorides at or above the high threshold.
ENVIRONMENT_RULES = (("co2_pct", "co2_high", "Повышенная доля CO₂"),
                     ("water_cut_pct", "water_cut_high", "Высокая обводнённость"),
                     ("chlorides_mg_l", "chlorides_high", "Высокая концентрация хлоридов"))


def _environment(features: Features) -> tuple[bool | None, list[str]]:
    """(corrosive, reasons); corrosive is None without any of the three values or their trends."""
    values, trends = features.current_values, features.trends_per_day
    reasons = [f"{reason}: {values[field]:g}." for field, high, reason in ENVIRONMENT_RULES
               if values.get(field) is not None and values[field] >= THRESHOLDS[high]]
    if reasons:
        return True, reasons
    known = (any(values.get(field) is not None for field, _, _ in ENVIRONMENT_RULES)
             or any(trends.get(field) is not None for field in ("co2_pct", "water_cut_pct")))
    return (False if known else None), []


# «Полнота данных» is judged on the columns of the rating table (rating.GAP_LABELS), by the values the
# well actually has — whatever brought them (ДАРС, ХИМИК, a protocol, the НКТ registry, a passport).
ENVIRONMENT_FIELDS = (("co2_pct", "CO₂"), ("water_cut_pct", "обводнённость"), ("chlorides_mg_l", "хлориды"))


def _fresh_window() -> timedelta:
    """A value older than this (from now) is outdated: the sample window or the telemetry age, the longer."""
    return max(timedelta(days=settings.rating_sampling_days), timedelta(hours=settings.max_telemetry_age_hours))


def _measured(field: str, name: str, records: list[TelemetryPoint], current: dict, now: datetime,
              in_calculation: bool = True) -> dict | None:
    """None when the well has a current value of the field; else {"field", "last"}: last is the date of
    the latest value when there is one but it is too old to count, None when there never was one.
    in_calculation: the value must also be among the current values the analysis used."""
    _, point = _latest(records, field)
    if point is None:
        return {"field": name, "last": None}
    if (in_calculation and current.get(field) is None) or now - _utc(point.timestamp) > _fresh_window():
        return {"field": name, "last": f"{_local(point.timestamp):%d.%m.%Y}"}
    return None


def _fields_note(items: list[dict]) -> str:
    absent = [item["field"] for item in items if not item.get("last")]
    old = [f"{item['field']} (последнее значение {item['last']})" for item in items if item.get("last")]
    parts = (["нет: " + ", ".join(absent)] if absent else []) + (["устарело: " + ", ".join(old)] if old else [])
    return "; ".join(parts)


def _forecast_gap(values: dict, records: list[TelemetryPoint], ready: readiness.Readiness) -> dict | None:
    """Why the index and the НТЦ forecast are empty, by count and by the inputs the well has."""
    if values.get("model_ntc") is not None:
        return None
    reasons: list[str] = []
    if not records:
        reasons.append("нет ни одного замера показателей")
    elif not ready.ready:
        if ready.unique_timestamps < settings.min_telemetry_records:
            reasons.append(f"замеров с разными датами — {ready.unique_timestamps} из {settings.min_telemetry_records} "
                           "нужных")
        if ready.recent_observations < settings.min_trend_observations:
            reasons.append(f"замеров за последние {settings.trend_window_days} сут. — {ready.recent_observations} "
                           f"из {settings.min_trend_observations} нужных")
        signals = ready.current_signals
        if len(signals) < settings.min_current_signals:
            names = readiness.SIGNAL_NAMES
            present = [names[field] for field in signals]
            suitable = [name for field, name in list(names.items())[:readiness.MAIN_SIGNALS] if field not in signals]
            reasons.append(f"показателей за последние {settings.max_forward_fill_days} сут. — "
                           f"{len(signals)} из {settings.min_current_signals} нужных"
                           + (f" (есть: {', '.join(present)})" if present else "")
                           + f"; подходят: {', '.join(suitable)}")
        if ready.ahead_hours is not None:
            reasons.append(f"Последнее измерение датировано будущим временем (+{ready.ahead_hours:.1f} ч.; "
                           f"допустимо до {settings.max_future_skew_hours:g} ч.); проверьте часы и часовой пояс "
                           "источника.")
    note = ("модели не рассчитаны: " + "; ".join(reasons)) if reasons else "модели не рассчитаны: пересчитайте скважину"
    return {"key": "forecast", "note": note, "items": [{"field": reason, "last": None} for reason in reasons]}


def _data_gaps(values: dict, records: list[TelemetryPoint], current: dict, ready: readiness.Readiness,
               lab_points: list[TelemetryPoint], demounts: list[dict], base_inspections: list[dict],
               now: datetime) -> list[dict]:
    """«Полнота данных»: per column of the rating table, the values it shows that the well lacks —
    never received, or outdated (with the date of the last value). [{"key", "note", "items"}]."""
    gaps: list[dict] = []

    def add(key: str, items: list[dict], note: str | None = None) -> None:
        if items:
            gaps.append({"key": key, "note": note or _fields_note(items), "items": items})

    add("well", ([] if values.get("field") else [{"field": "месторождение", "last": None}])
        + ([] if values.get("in_operation") is not None else [{"field": "статус работы (в работе или нет)", "last": None}]))
    forecast = _forecast_gap(values, records, ready)
    if forecast:
        gaps.append(forecast)
    rate = _measured("corrosion_rate_mm_year", "скорость коррозии", records, current, now)
    if values.get("category") is None:
        add("category", [rate or {"field": "признаки коррозии", "last": None}],
            "не определена: нет оценки среды, скорости коррозии и актов с признаком коррозии")
    elif rate:
        add("category", [rate])
    add("environment", [item for field, name in ENVIRONMENT_FIELDS
                        if (item := _measured(field, name, records, current, now))])
    if values.get("sampling") is not True:
        last = f"{_local(lab_points[-1].timestamp):%d.%m.%Y}" if lab_points else None
        add("sampling", [{"field": f"лабораторная проба за {settings.rating_sampling_days} сут.", "last": last}],
            f"нет лабораторной пробы за {settings.rating_sampling_days} сут."
            + (f" (последняя — {last})" if last else " (проб нет)"))
    acts: list[dict] = []
    if demounts:
        last_demount = _utc(demounts[-1]["event_date"])
        if not any(_utc(event["event_date"]) >= last_demount for event in base_inspections):
            acts.append({"field": f"акт разбора на базе после демонтажа {_local(last_demount):%d.%m.%Y}", "last": None})
    for act, corrosion, name in (("demount_act", "demount_corrosion", "признак коррозии в акте демонтажа"),
                                 ("base_act", "base_corrosion", "признак коррозии в акте разбора на базе")):
        if values.get(act) and values.get(corrosion) is None:
            acts.append({"field": name, "last": None})
    add("acts", acts)
    protection: list[dict] = []
    if not values.get("equipment"):
        protection.append({"field": "спущенное оборудование (насос, НКТ, исполнение)", "last": None})
    if values.get("protection") is None:
        protection.append(_measured("inhibitor_efficiency", "эффективность ингибитора", records, current, now)
                          or {"field": "эффективность ингибитора", "last": None})
    elif current.get("inhibitor_efficiency") is not None:
        deviation = _measured("injection_deviation_pct", "отклонение закачки ингибитора", records, current, now)
        if deviation:
            protection.append(deviation)
    if values.get("protection") == "unconfirmed" and rate:
        protection.append({**rate, "field": "скорость коррозии (подтверждение защиты)"})
    add("protection", protection)
    if values.get("installed_at") is None:
        add("run_days", [{"field": "дата спуска оборудования", "last": None}])
    liquid = [item for item in (_measured("liquid_rate_m3_day", "дебит жидкости", records, current, now,
                                          in_calculation=False),) if item]
    if values.get("oil_pct") is None:
        liquid.append({"field": "обводнённость (для доли нефти)", "last": None})
    add("liquid", liquid)
    return gaps


def _ntc_score(findings: list[Finding] | None, when: datetime, sources: _Sources,
               protection: str | None) -> tuple[dict, dict]:
    """The НТЦ score from the rule points. With confirmed effective protection (and
    MAI_RATING_PROTECTION_MODE = adjust) the environment rule points count with
    MAI_RATING_PROTECTION_FACTOR; points for measured damage are never reduced."""
    values = {"model_ntc": None, "model_ntc_raw": None}
    if findings is None:
        return values, {"model_ntc": _prov(sources, None, when, "Недостаточно данных для правил НТЦ.")}
    points = sum(item.points for item in findings)
    env_points = sum(item.points for item in findings if item.rule_id.startswith(ENVIRONMENT_PREFIX))
    values["model_ntc"] = values["model_ntc_raw"] = score(points)
    note = f"Правила НТЦ: {points} баллов из 14."
    if protection == "effective" and env_points:
        factor = settings.rating_protection_factor
        adjusted_points = points - env_points * (1 - factor)
        if settings.rating_protection_mode == "adjust":
            values["model_ntc"] = score(adjusted_points)
            note += (f" Защита эффективна: баллы среды ({env_points}) учтены с коэффициентом "
                     f"{factor:g}, итого {adjusted_points:g} баллов.")
        else:
            note += (f" Защита эффективна; с её учётом было бы {adjusted_points:g} баллов "
                     "(режим MAI_RATING_PROTECTION_MODE=info, индекс не корректируется).")
    return values, {"model_ntc": _prov(sources, None, when, note)}


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


def build_values(records: list[TelemetryPoint], events: list[dict], now: datetime | None = None,
                 passports: list[tuple[str, dict, datetime | None]] | None = None) -> tuple[dict, dict]:
    """Rating field values and their provenance for one well."""
    now = now or datetime.now(timezone.utc)
    sources = _Sources()
    records = sorted(records, key=lambda point: _utc(point.timestamp))
    prepared = prepare(records) if records else None
    ready = readiness.assess(records)
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
    from app.core.source_kinds import is_snapshot, snapshot_source_ids

    snapshot_ids = snapshot_source_ids()
    measured = [point for point in records if not is_snapshot(point, snapshot_ids)]
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

    # Environment: CO₂, water cut and chlorides of the latest values.
    rate = None
    if prepared is not None:
        values["corrosive_environment"], found = _environment(prepared.features)
        reasons = "; ".join(found)
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

    values.update(model_ntc=None, model_ntc_raw=None)
    if prepared is not None and ready.ready:
        scores, score_prov = _ntc_score(evaluate_rules(prepared.features, prepared.normalized),
                                        prepared.features.latest_timestamp, sources, protection)
        values.update(scores)
        prov.update(score_prov)

    # «Полнота данных»: per column of the rating, the values the well lacks (not the sources).
    demounts = [event for event in by_kind["demount"] if _utc(event["event_date"]) >= window_start]
    values["data_gaps"] = _data_gaps(values, records, current, ready, lab_points, demounts,
                                     by_kind["base_inspection"], now)
    return values, prov


def _assess_protection(values: dict, prepared: Prepared | None, rate: float | None,
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


def update(well_id: str) -> dict:
    """Recompute the well's rating from its stored data and store it; returns the stored row."""
    values, provenance = build_values(db.load_telemetry(well_id), db.load_history_events(well_id),
                                      passports=well_passports(well_id))
    return db.save_computed_rating(well_id, values, provenance=provenance,
                                   measure=rating.measure_for(rating.priority_index(values["model_ntc"])))


def recompute_wells(well_ids: Iterable[str]) -> dict:
    """Recompute the rating of the wells from stored data. Used after uploads and pushes; no source
    APIs are called (the daily cycle and the per-well button refresh sources first)."""
    done, failed = [], []
    for well_id in dict.fromkeys(well_ids):
        try:
            update(well_id)
            done.append(well_id)
        except Exception:
            logger.exception("Rating recompute failed for well_id=%s", well_id)
            failed.append(well_id)
    return {"recomputed": done, "failed": failed}


def schedule_recompute(well_ids: Iterable[str], background_tasks) -> dict:
    """Recompute now for small uploads, in the background for large ones."""
    wells = [well for well in dict.fromkeys(well_ids) if well]
    if not wells:
        return {"mode": "none", "wells": 0}
    if len(wells) <= settings.rating_recompute_sync_limit or background_tasks is None:
        outcome = recompute_wells(wells)
        return {"mode": "sync", "wells": len(wells), "failed": outcome["failed"]}
    background_tasks.add_task(recompute_wells, wells)
    return {"mode": "background", "wells": len(wells)}
