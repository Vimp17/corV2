"""AI agent's proposal for one well: what to do, by when, which crew, and why.

The LLM (LiteLLM, MAI_LLM_MODEL) receives everything the system knows about the well — the
rating and the НТЦ forecast, acts, chemistry, protection, data gaps, documents and their
status, the crew schedule — plus the response-measure examples of config/response_examples.json.
It must rely on that data only and name what is missing instead of guessing. The answer is
checked (measure code, crew from the schedule, dates) and stored on the well's rating row.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any

from app import db
from app.config import settings
from app.core import llm, proposal_language, rating

logger = logging.getLogger(__name__)

MEASURE_CODES = ("ППР", "РИ", "УДР", "—")
LAB_FIELDS = ("water_cut_pct", "co2_pct", "chlorides_mg_l", "inhibitor_efficiency", "injection_deviation_pct",
              "corrosion_rate_mm_year", "wall_thickness_mm")
FIELD_LABELS = {
    "water_cut_pct": "обводнённость, %", "co2_pct": "CO₂, %", "chlorides_mg_l": "хлориды, мг/л",
    "inhibitor_efficiency": "эффективность ингибитора (доля)", "injection_deviation_pct": "отклонение закачки, %",
    "corrosion_rate_mm_year": "скорость коррозии, мм/год", "wall_thickness_mm": "толщина стенки, мм",
}

SYSTEM_PROMPT = (
    "Ты — инженер-технолог по защите скважинного оборудования от коррозии. По данным одной скважины "
    "подготовь предложение по мерам реагирования. Ответ — только JSON без пояснений.\n"
    "Правила:\n"
    "1. Пиши только на русском языке, грамотным техническим языком, как в служебной записке инженера. "
    "Не используй английские слова и названия полей или ключей из входных данных. Латиница допустима "
    "только в химических формулах и обозначениях (CO₂, H₂S, pH) и в номерах скважин, бригад, документов.\n"
    "2. Опирайся только на переданные данные и не додумывай значения.\n"
    "3. measure — один код: ППР (плановый комплекс по ремонту, включение в ЭРА ремонта), РИ (решение "
    "инженера), УДР (ударная дозировка реагента), — (мероприятия не требуются). По умолчанию бери код "
    "мероприятия рейтинга; другой предлагай, только если данные это прямо обосновывают, и объясни "
    "почему в reasoning.\n"
    "4. Бригаду предлагай только из списка «бригады» со статусом «свободна» и свободную до крайнего "
    "срока; иначе crew: null.\n"
    "5. deadline — дата ГГГГ-ММ-ДД не позже крайнего срока рейтинга, если он задан.\n"
    "6. Действия формулируй по образцу «примеры решений инженеров» для похожих ситуаций.\n"
    "7. based_on — какие данные использованы, с датами (например «акт разбора от 12.03.2026: язвенная "
    "коррозия»).\n"
    "8. Акты демонтажа и разбора — исторические данные: события из истории ремонтов, а не текущее "
    "состояние скважины. Так и пиши: «по акту разбора от …».\n"
    "9. «Другие показатели» и «реестр коррозии НКТ» в рейтинге не учитываются; используй их как "
    "дополнительный контекст и прямо укажи, если опираешься на них.\n"
    "10. missing_data — только пробелы, которых нет в списке «уже известные пробелы в данных» (он будет "
    "добавлен к ответу сам): конкретные поля или значения, которых не хватает для решения, а не "
    "названия систем-источников. Если новых пробелов нет — пустой список.\n"
    'Формат (ключи — как здесь, все значения — по-русски): {"measure": "...", "summary": "одно предложение", '
    '"actions": ["..."], "deadline": "ГГГГ-ММ-ДД" или null, "crew": {"crew_id": "...", "date": "ГГГГ-ММ-ДД"} '
    'или null, "reasoning": "...", "missing_data": ["..."], "based_on": ["..."]}'
)


class ProposalUnavailable(RuntimeError):
    """The LLM is not configured; status_code tells the API how to answer."""

    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


def configured() -> bool:
    return bool(settings.llm_api_url and settings.llm_model)


@lru_cache(maxsize=4)
def _examples(path: str, mtime: float) -> list[dict]:
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError):
        logger.warning("Response examples could not be read from %s", path)
        return []
    items = data.get("examples") if isinstance(data, dict) else None
    return [item for item in items or [] if isinstance(item, dict)][:30]


def response_examples() -> list[dict]:
    path = settings.response_examples_path
    try:
        return _examples(str(path), path.stat().st_mtime)
    except OSError:
        return []


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)[:10]


def build_context(well_id: str) -> dict | None:
    """Everything known about the well, in plain words and numbers for the LLM."""
    stored = db.get_rating_row(well_id)
    if stored is None:
        return None
    from app.core.rating_builder import _Sources  # source titles, lab sources, act kinds

    sources = _Sources()
    row = rating.build_rating([stored])[0]
    provenance = row.get("provenance") or {}
    notes = {key: (provenance.get(key) or {}).get("note") for key in
             ("category", "corrosive_environment", "protection", "demount_act", "demount_corrosion",
              "base_act", "base_corrosion", "model_ntc", "sampling")}
    context: dict[str, Any] = {
        "well_id": well_id,
        "today": rating.local_today().isoformat(),
        "rating": {
            "index": row.get("index"), "index_before_protection": row.get("index_raw"),
            "measure_code": row.get("measure_code"), "measure": row.get("measure_label"),
            "deadline": row.get("deadline"), "plan_date": row.get("plan_date"),
            "forecast_ntc": row.get("model_ntc"),
            "category": row.get("category_label") or None,
            "corrosive_environment": row.get("corrosive_environment"),
            "protection": row.get("protection_label") or None,
            "protection_means": row.get("protection_means"),
            "field": row.get("field"), "equipment": row.get("equipment"), "run_days": row.get("run_days"),
            "liquid_m3_day": row.get("liquid_rate_m3_day"), "oil_pct": row.get("oil_pct"),
            "in_operation": row.get("in_operation"),
            "demount_act_recent": row.get("demount_act"), "demount_corrosion": row.get("demount_corrosion"),
            "base_act_recent": row.get("base_act"), "base_corrosion": row.get("base_corrosion"),
            "data_gaps": [f"{rating.GAP_LABELS.get(item.get('key'), item.get('key'))}: {item.get('note')}"
                          for item in row.get("data_gaps") or []],
            "notes": {key: value for key, value in notes.items() if value},
        },
    }
    acts, failures = [], []
    for event in db.load_history_events(well_id):
        payload = event["payload"]
        if event["event_type"] == "work":
            kind = sources.classify(payload.get("work_type"))
            if kind in {"demount", "base_inspection"}:
                acts.append({"date": _iso(event["event_date"]), "type": payload.get("work_type"),
                             "equipment": payload.get("equipment"), "result": payload.get("result"),
                             "cause": payload.get("reason"), "corrosion_detected": payload.get("corrosion_detected"),
                             "source": sources.title(event["source_id"])})
        else:
            failures.append({"date": _iso(event["event_date"]), "failure": payload.get("failure_type"),
                             "cause": payload.get("cause"), "corrosion_detected": payload.get("corrosion_detected")})
    context["acts"] = acts[-6:]
    context["failures"] = failures[-4:]

    samples, latest = [], {}
    for point in db.load_telemetry(well_id):
        values = {FIELD_LABELS[name]: getattr(point, name) for name in LAB_FIELDS if getattr(point, name, None) is not None}
        for name, value in values.items():
            latest[name] = value
        point_sources = set(str(getattr(point, "source_id", "") or "").split("+"))
        if values and sources.lab_ids & point_sources:
            samples.append({"date": _iso(point.timestamp), "source": sources.title(getattr(point, "source_id", "")),
                            **values})
    context["lab_samples"] = samples[-4:]
    context["latest_measurements"] = latest
    # pH, hardness and other indicators the system has no field for: context only.
    from app.core.rating_builder import extra_measurements

    context["other_measurements"] = [{"name": item["name"], "value": item["value"], "date": _iso(item["date"]),
                                      "source": item["source"]} for item in extra_measurements(well_id)]

    # Реестр коррозии НКТ: the well's row(s) as in the file — suspension, steel, failures, damage.
    from app.core.documents.nkt_registry import well_registry

    registry = well_registry(well_id)
    context["nkt_registry"] = ({"date": registry["date"], "rows": [dict(row) for row in registry["rows"][:5]]}
                               if registry else None)

    titles = {}
    try:
        from app.core.documents.types import load_document_types

        titles = {key: spec.get("title") for key, spec in load_document_types().items()}
    except Exception:
        titles = {}
    from app.core.documents.pipeline import STATUS_LABELS

    context["documents"] = [{"type": titles.get(item.get("doc_type") or "", item.get("doc_type")),
                             "file": item.get("filename"), "date": item.get("doc_date"),
                             "status": STATUS_LABELS.get(item.get("status"), item.get("status")),
                             "note": item.get("status_message")}
                            for item in db.list_documents(well_id=well_id, limit=20)]
    crews = []
    for payload in db.load_source_payloads(well_id).values():
        for item in payload.get("crew_availability") or []:
            if isinstance(item, dict):
                crews.append({key: item.get(key) for key in
                              ("crew_id", "specialties", "region", "status", "available_from", "available_to")})
    context["crews"] = crews[:100]
    return context


def missing_from_context(context: dict) -> list[str]:
    """What is missing, decided without the LLM, so it is reported even if the model omits it: concrete
    fields and values (app.core.proposal_language.field_gaps)."""
    return proposal_language.field_gaps(context, settings.rating_act_days)


def _check(answer: dict, context: dict) -> dict:
    rating_code = context["rating"].get("measure_code") or None
    notes: list[str] = []
    measure = str(answer.get("measure") or "").strip()
    if measure == "-":
        measure = "—"
    if measure not in MEASURE_CODES:
        if measure:
            notes.append(f"Модель предложила неизвестное мероприятие «{measure[:40]}» — взято мероприятие рейтинга.")
        measure = rating_code or "—"
    crew = answer.get("crew") if isinstance(answer.get("crew"), dict) else None
    known_crews = {str(item.get("crew_id")): item for item in context["crews"] if item.get("crew_id")}
    if crew is not None:
        crew_id = str(crew.get("crew_id") or "").strip()
        if crew_id not in known_crews:
            notes.append(f"Предложенной бригады «{crew_id[:60]}» нет в расписании — бригада не назначена.")
            crew = None
        else:
            crew = {"crew_id": crew_id, "date": _date_or_none(crew.get("date")),
                    "specialties": known_crews[crew_id].get("specialties") or []}
    if measure == "—" and crew is not None:
        notes.append("Мероприятия не требуются — бригада не назначается.")
        crew = None
    deadline = _date_or_none(answer.get("deadline"))
    rating_deadline = _date_or_none(context["rating"].get("deadline"))
    if deadline and rating_deadline and deadline > rating_deadline:
        notes.append(f"Срок модели позже срока рейтинга — взят срок рейтинга {rating_deadline}.")
        deadline = rating_deadline
    deadline = deadline or rating_deadline

    def strings(value, limit: int, size: int = 500) -> list[str]:
        items = value if isinstance(value, list) else ([value] if value else [])
        return [str(item).strip()[:size] for item in items if str(item or "").strip()][:limit]

    # Known gaps first; the model's items only when they are not a rephrasing of one of them.
    missing = proposal_language.merge_gaps(missing_from_context(context),
                                           [proposal_language.clean(item) for item in strings(answer.get("missing_data"), 15)])
    clean = proposal_language.clean
    return {
        "measure": measure,
        "rating_measure": rating_code,
        "differs_from_rating": bool(rating_code) and measure != rating_code,
        "summary": clean(str(answer.get("summary") or "").strip())[:600],
        "actions": [clean(item) for item in strings(answer.get("actions"), 10)],
        "deadline": deadline,
        "crew": crew,
        "reasoning": clean(str(answer.get("reasoning") or "").strip())[:3000],
        "missing_data": missing,
        "based_on": [clean(item) for item in strings(answer.get("based_on"), 15, 300)],
        "checks": notes,
        "index": context["rating"].get("index"),
    }


def _date_or_none(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat() if value else None
    except ValueError:
        return None


def generate(well_id: str) -> dict:
    """Draft, check and store the proposal; returns it. Raises ProposalUnavailable / LookupError."""
    if not configured():
        raise ProposalUnavailable("ИИ-агент не настроен: задайте MAI_LLM_API_URL и MAI_LLM_MODEL.")
    context = build_context(well_id)
    if context is None:
        raise LookupError(well_id)
    # The model reads the well in Russian (names and values) and answers in the same language.
    payload = proposal_language.for_model(context, missing_from_context(context), response_examples(),
                                          settings.rating_act_days)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]
    try:
        answer = llm.parse_json_object(llm.chat(messages, model=settings.llm_model, temperature=0.2))
    except llm.LLMError as exc:
        raise ProposalUnavailable(f"ИИ-агент недоступен: {exc}", status_code=502) from exc
    except ValueError as exc:
        raise ProposalUnavailable("ИИ-агент вернул ответ не в формате JSON.", status_code=502) from exc
    proposal = {**_check(answer, context), "model": settings.llm_model,
                "generated_at": datetime.now(timezone.utc).isoformat()}
    db.save_ai_proposal(well_id, proposal)
    return proposal
