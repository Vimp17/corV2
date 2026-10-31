"""Russian wording of the AI agent's proposal: what the model reads and the «Нет данных» list.

The model answers in the language of its input. app.core.proposal_agent keeps its context with
English keys for the checks; the model receives it with Russian names and values (for_model).
Service words that still slip into the answer are replaced with Russian ones (clean). «Нет
данных» names concrete fields the system knows to be missing (field_gaps), and the model's own
items are added only when they are new (merge_gaps): rephrasings of a known gap are dropped.
"""
from __future__ import annotations

import re
from typing import Any

CREW_STATUS = {"available": "свободна", "busy": "занята", "unknown": "неизвестно"}
NOTE_NAMES = {"category": "категория фонда", "corrosive_environment": "среда", "protection": "защита",
              "demount_act": "акт демонтажа", "demount_corrosion": "коррозия при демонтаже",
              "base_act": "акт разбора на базе", "base_corrosion": "коррозия по акту разбора",
              "model_ntc": "модель НТЦ", "model_math": "математическая модель", "sampling": "отбор проб"}
# Service words of the input and common English terms the model may still write: → Russian.
GLOSSARY = {
    "measure_code": "код мероприятия", "index_before_protection": "индекс без учёта защиты",
    "forecast_ntc": "прогноз модели НТЦ", "forecast_math": "прогноз математической модели",
    "model_ntc": "модель НТЦ", "model_math": "математическая модель", "plan_date": "плановая дата",
    "corrosive_environment": "коррозионная среда", "protection_means": "средства защиты",
    "in_operation": "в работе", "run_days": "наработка", "liquid_m3_day": "дебит жидкости", "oil_pct": "доля нефти",
    "demount_act_recent": "акт демонтажа", "demount_act": "акт демонтажа", "demount_corrosion": "коррозия при демонтаже",
    "base_act_recent": "акт разбора на базе", "base_act": "акт разбора на базе",
    "base_corrosion": "коррозия по акту разбора", "data_gaps": "пробелы в данных", "missing_data": "нет данных",
    "lab_samples": "лабораторные пробы", "latest_measurements": "последние значения показателей",
    "other_measurements": "другие показатели", "nkt_registry": "реестр коррозии НКТ",
    "response_examples": "примеры решений", "crew_id": "бригада", "crews": "бригады", "crew": "бригада",
    "deadline": "крайний срок", "measure": "мероприятие", "rating": "рейтинг", "available": "свободна",
    "busy": "занята", "workover": "ремонт скважины", "inhibitor": "ингибитор", "water cut": "обводнённость",
    "corrosion rate": "скорость коррозии", "tubing": "НКТ", "ERA": "ЭРА",
}
_GLOSSARY_LOWER = {word.lower(): russian for word, russian in GLOSSARY.items()}
_GLOSSARY_PATTERN = re.compile(r"(?<![A-Za-z_])(" + "|".join(
    re.escape(word) for word in sorted(GLOSSARY, key=len, reverse=True)) + r")(?![A-Za-z_])", re.IGNORECASE)
_WORD = re.compile(r"[0-9a-zа-яё₂]+")
# Stems that only say «something is missing»: «Отсутствует расписание бригад» repeats «Нет расписания бригад».
_FILLER = {"нет", "отсутс", "данных", "данные", "информ", "сведен", "значен", "поэтом", "также", "требуе",
           "необхо", "для", "что", "или", "это", "его", "так"}


def _yes_no(value: Any, yes: str = "да", no: str = "нет") -> str:
    return "нет данных" if value is None else (yes if value else no)


def clean(text: Any) -> str:
    """The text with service words (field names of the input, English terms) put into Russian."""
    return _GLOSSARY_PATTERN.sub(lambda match: _GLOSSARY_LOWER[match.group(1).lower()], str(text))


def _stem(word: str) -> str:
    """A word without its ending: the first six letters of a long word, a short one without its last
    letter («дата», «дате» → «дат»); numbers stay whole."""
    if word.isdigit():
        return word
    return word[:6] if len(word) >= 7 else word[:max(3, len(word) - 1)]


def _key(text: str) -> set[str]:
    """The words of a gap, cut to their stems: «Нет пробы» and «Нет проб» are the same gap."""
    return {_stem(word) for word in _WORD.findall(text.casefold().replace("ё", "е"))
            if len(word) > 2 and word[:6] not in _FILLER}


def merge_gaps(known: list[str], extra: list[str]) -> list[str]:
    """Known gaps first, then the model's items that are not a rephrasing of one already listed."""
    result: list[str] = []
    keys: list[set[str]] = []
    for item in [*known, *extra]:
        item = " ".join(str(item).split())
        words = _key(item)
        if not words or any(words <= other or other <= words or len(words & other) / len(words | other) >= 0.6
                            for other in keys):
            continue
        result.append(item)
        keys.append(words)
    return result


def field_gaps(context: dict, act_days: int) -> list[str]:
    """What is missing for a decision, as concrete fields (not the systems they come from): the gaps
    of the rating columns (fields and dates, app.core.rating_builder._data_gaps), then what only the
    agent looks at — acts in the repair history, the crew schedule, unreadable documents."""
    rating = context["rating"]
    gaps = list(rating.get("data_gaps") or [])
    if not context.get("acts"):
        gaps.append(f"Нет актов демонтажа и разбора за {act_days} сут. в истории ремонтов.")
    if not context.get("crews"):
        gaps.append("Нет расписания бригад — бригада не назначена.")
    for document in context.get("documents") or []:
        if document.get("status") in {"Не распознан", "Ошибка"}:
            gaps.append(f"Документ «{document.get('file')}» не распознан — его данные не учтены.")
    return merge_gaps(gaps, [])


def for_model(context: dict, known_gaps: list[str], examples: list[dict], act_days: int) -> dict:
    """The context with Russian names and values: the language the answer should be written in."""
    rating = context["rating"]
    return {
        "скважина": context["well_id"],
        "сегодня": context["today"],
        "рейтинг": {
            "индекс коррозии на НКТ (0–100)": rating.get("index"),
            "индекс без учёта защиты": rating.get("index_before_protection"),
            "код мероприятия": rating.get("measure_code") or "нет оценки",
            "мероприятие": rating.get("measure"),
            "крайний срок": rating.get("deadline"),
            "плановая дата ППР": rating.get("plan_date"),
            "прогноз модели НТЦ (0–1)": rating.get("forecast_ntc"),
            "прогноз математической модели (0–1)": rating.get("forecast_math"),
            "категория фонда": rating.get("category"),
            "коррозионная среда": _yes_no(rating.get("corrosive_environment")),
            "защита от коррозии": rating.get("protection"),
            "средства защиты": rating.get("protection_means") or [],
            "месторождение": rating.get("field"),
            "спущенное оборудование": rating.get("equipment"),
            "наработка, сут": rating.get("run_days"),
            "дебит жидкости, м³/сут": rating.get("liquid_m3_day"),
            "доля нефти, %": rating.get("oil_pct"),
            "в работе": _yes_no(rating.get("in_operation")),
            "пояснения расчёта": {NOTE_NAMES.get(key, key): value for key, value in (rating.get("notes") or {}).items()},
        },
        f"исторические акты демонтажа и разбора (последние за {act_days} сут.)": {
            "акт демонтажа": _yes_no(rating.get("demount_act_recent"), "есть", "нет"),
            "коррозия при демонтаже": _yes_no(rating.get("demount_corrosion"), "обнаружена", "не обнаружена"),
            "акт разбора на базе": _yes_no(rating.get("base_act_recent"), "есть", "нет"),
            "коррозия по акту разбора": _yes_no(rating.get("base_corrosion"), "обнаружена", "не обнаружена"),
        },
        "акты из истории ремонтов": [{
            "дата": act.get("date"), "вид": act.get("type"), "оборудование": act.get("equipment"),
            "результат": act.get("result"), "причина": act.get("cause"),
            "коррозия": _yes_no(act.get("corrosion_detected"), "обнаружена", "не обнаружена"),
            "источник": act.get("source")} for act in context.get("acts") or []],
        "отказы": [{"дата": item.get("date"), "отказ": item.get("failure"), "причина": item.get("cause"),
                    "коррозия": _yes_no(item.get("corrosion_detected"), "обнаружена", "не обнаружена")}
                   for item in context.get("failures") or []],
        "лабораторные пробы": [{("дата" if key == "date" else "источник" if key == "source" else key): value
                                for key, value in sample.items()} for sample in context.get("lab_samples") or []],
        "последние значения показателей": context.get("latest_measurements") or {},
        "другие показатели (в рейтинге не учитываются)": [{
            "показатель": item.get("name"), "значение": item.get("value"), "дата": item.get("date"),
            "источник": item.get("source")} for item in context.get("other_measurements") or []],
        "реестр коррозии НКТ (в рейтинге не учитывается, кроме среды и дебита)": (
            {"дата реестра": context["nkt_registry"].get("date"), "строки": context["nkt_registry"].get("rows")}
            if context.get("nkt_registry") else None),
        "документы скважины": [{"вид": item.get("type"), "файл": item.get("file"), "дата": item.get("date"),
                                "статус": item.get("status"), "примечание": item.get("note")}
                               for item in context.get("documents") or []],
        "бригады": [{"бригада": crew.get("crew_id"), "специальности": crew.get("specialties"),
                     "регион": crew.get("region"),
                     "статус": CREW_STATUS.get(str(crew.get("status") or "").lower(), crew.get("status")),
                     "свободна с": crew.get("available_from"), "свободна до": crew.get("available_to")}
                    for crew in context.get("crews") or []],
        "уже известные пробелы в данных": known_gaps,
        "примеры решений инженеров": [{"ситуация": item.get("situation"), "мероприятие": item.get("measure"),
                                       "действия": item.get("actions"), "почему": item.get("why")}
                                      for item in examples],
    }
