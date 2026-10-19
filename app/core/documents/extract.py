"""Document type, field extraction by the LLM, and the checks that decide what is loaded.

The LLM (LiteLLM Chat Completions, MAI_DOCS_LLM_MODEL or MAI_LLM_MODEL) returns every value
with a quote copied from the document. A value is kept only when its quote is found in the
document text and agrees with the value; a record is accepted only when its required fields
survive, the well is known and the date is plausible. Nothing is guessed: a document whose
data cannot be confirmed loads nothing and its data counts as missing.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from app.config import settings
from app.core import llm

logger = logging.getLogger(__name__)

EARLIEST_DATE = date(2000, 1, 1)
MAX_QUOTE_CHARS = 300
_WELL_PREFIX = re.compile(r"^(?:скв(?:ажина)?\.?|well)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


class ExtractionUnavailable(RuntimeError):
    """The LLM is not configured or not reachable; the document can be retried later."""


# --------------------------------------------------------------------------- text helpers
def normalize(text: Any) -> str:
    """Lower case, ё→е, no table pipes / emphasis, unified quotes and dashes, single spaces."""
    value = str(text or "").casefold().replace("ё", "е")
    value = re.sub(r"[|*_`#]", " ", value)
    value = value.translate(str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "'": '"',
                                           "–": "-", "—": "-", " ": " "}))
    return re.sub(r"\s+", " ", value).strip()


def quote_found(quote: str, normalized_text: str) -> bool:
    """The quote occurs in the text, allowing small OCR / spacing differences."""
    needle = normalize(quote)
    if len(needle) < 2:
        return False
    if needle in normalized_text:
        return True
    if len(needle) < 8:
        return False
    # Fuzzy matches tolerate OCR noise in words, never in numbers: every token with a digit
    # (well number, date, value) must occur in the text exactly.
    for token in re.findall(r"\w*\d\w*", needle):
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", normalized_text) is None:
            return False
    match = difflib.SequenceMatcher(None, normalized_text, needle, autojunk=False).find_longest_match(
        0, len(normalized_text), 0, len(needle))
    if match.size >= 0.9 * len(needle):
        return True
    # OCR may break a quote across a line or table cell: check its words appear close together.
    words = [word for word in re.findall(r"\w+", needle) if len(word) > 1]
    if len(words) < 2:
        return False
    first = normalized_text.find(words[0])
    while first >= 0:
        window = normalized_text[first:first + len(needle) * 2 + 40]
        if all(word in window for word in words):
            return True
        first = normalized_text.find(words[0], first + 1)
    return False


def _numbers_in(text: str) -> list[float]:
    compact = re.sub(r"(?<=\d)[\s ](?=\d{3}\b)", "", str(text))
    values = []
    for item in _NUMBER.findall(compact):
        try:
            values.append(float(item.replace(",", ".")))
        except ValueError:
            continue
    return values


def clean_well_id(value: Any) -> str:
    text = str(value or "").strip()
    text = _WELL_PREFIX.sub("", text)
    text = text.replace("№", "").replace("#", "").strip(" .:-")
    return re.sub(r"\s+", "", text).upper()[:128]


def match_well(value: Any, known: list[str]) -> str | None:
    """The known well the document refers to: same id ignoring case, spaces and «скв. №»;
    otherwise a unique well with the same digits. Without a well list the cleaned id is used."""
    cleaned = clean_well_id(value)
    if not cleaned:
        return None
    if not known:
        return cleaned
    by_key = {clean_well_id(item): item for item in known}
    if cleaned in by_key:
        return by_key[cleaned]
    digits = re.sub(r"\D", "", cleaned)
    if digits:
        candidates = [item for key, item in by_key.items() if re.sub(r"\D", "", key).lstrip("0") == digits.lstrip("0")
                      and re.sub(r"\d", "", key) == re.sub(r"\d", "", cleaned)]
        if len(candidates) == 1:
            return candidates[0]
    return None


# --------------------------------------------------------------------------- value parsing
def parse_value(raw: Any, field_type: str) -> Any:
    """Typed value or None; raises ValueError for a value of the wrong type."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if field_type == "string":
        return str(raw).strip()[:2000]
    if field_type == "number":
        if isinstance(raw, bool):
            raise ValueError("not a number")
        if isinstance(raw, (int, float)):
            return float(raw)
        numbers = _numbers_in(str(raw))
        if len(numbers) != 1:
            raise ValueError("not a number")
        return numbers[0]
    if field_type == "boolean":
        if isinstance(raw, bool):
            return raw
        text = normalize(raw)
        if text in {"true", "да", "yes", "1"}:
            return True
        if text in {"false", "нет", "no", "0"}:
            return False
        raise ValueError("not a boolean")
    if field_type == "date":
        text = str(raw).strip()[:32]
        for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, pattern).date()
            except ValueError:
                continue
        raise ValueError("not a date")
    raise ValueError(f"unknown field type {field_type}")


_MONTHS = ["январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]


def value_agrees(value: Any, field_type: str, quote: str, field_name: str) -> bool:
    """The quote supports the value (the number, date or well id is written in it)."""
    text = normalize(quote)
    if field_type == "number":
        for number in _numbers_in(text):
            if abs(number - value) < 1e-6 or abs(number - value * 100) < 1e-6 or abs(number / 100 - value) < 1e-9:
                return True
        return False
    if field_type == "date":
        years = {str(value.year), f".{value.year % 100:02d}"}
        if not any(year in text for year in years):
            return False
        day_pattern = rf"(?<!\d)0?{value.day}(?!\d)"
        month_ok = (re.search(rf"(?<!\d)0?{value.month}(?!\d)", text) is not None
                    or _MONTHS[value.month - 1] in text)
        return re.search(day_pattern, text) is not None and month_ok
    if field_name == "well_id":
        digits = re.sub(r"\D", "", str(value))
        return not digits or digits in re.sub(r"\D", "", text)
    return True


# --------------------------------------------------------------------------- classification
def classify_by_keywords(text: str, types: dict) -> str | None:
    """The type whose keywords occur in the document; title words count three times."""
    body = normalize(text)
    head = body[:1500]
    scores = {}
    for type_id, spec in types.items():
        score = 0
        for keyword in spec.get("keywords", []):
            word = normalize(keyword)
            if not word:
                continue
            if word in head:
                score += 3
            elif word in body:
                score += 1
        if score:
            scores[type_id] = score
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def _llm_chat(messages: list[dict]) -> str:
    if not (settings.llm_api_url and settings.extraction_model):
        raise ExtractionUnavailable("Языковая модель не настроена (MAI_LLM_API_URL и MAI_DOCS_LLM_MODEL / MAI_LLM_MODEL).")
    try:
        return llm.chat(messages, model=settings.extraction_model, temperature=0.0)
    except llm.LLMError as exc:
        raise ExtractionUnavailable(f"Языковая модель недоступна: {exc}") from exc


def classify_by_llm(text: str, types: dict) -> str | None:
    catalogue = {type_id: f"{spec.get('title')}: {spec.get('description', '')}" for type_id, spec in types.items()}
    messages = [
        {"role": "system", "content": (
            "Определи тип документа нефтедобывающего предприятия по его тексту. Ответь только JSON "
            '{"type": "<код типа>"} с одним из кодов из списка или {"type": "other"}, если документ '
            "не относится ни к одному типу или текст нечитаем.")},
        {"role": "user", "content": json.dumps({"types": catalogue, "text": text[:6000]}, ensure_ascii=False)},
    ]
    try:
        answer = llm.parse_json_object(_llm_chat(messages))
    except ValueError:
        return None
    type_id = str(answer.get("type") or "").strip()
    return type_id if type_id in types else None


# --------------------------------------------------------------------------- extraction
EXTRACTION_SYSTEM_PROMPT = (
    "Ты извлекаешь данные из документа нефтедобывающего предприятия (акт, протокол, паспорт). "
    "Отвечай только JSON без пояснений. Правила:\n"
    "1. Бери значения только из текста документа. Ничего не додумывай и не вычисляй, кроме перевода "
    "единиц, прямо указанного в подсказке поля.\n"
    "2. Для каждого найденного значения укажи quote — короткую дословную цитату из текста (до 200 "
    "символов), где это значение записано; копируй символ в символ, без исправлений.\n"
    "3. Если значения в документе нет или оно нечитаемо — value: null и quote: null.\n"
    "4. Даты — в формате ГГГГ-ММ-ДД; числа — числом с точкой; логические поля — true, false или null.\n"
    "5. Если в документе несколько скважин или проб, верни отдельную запись на каждую."
)


def _field_catalogue(spec: dict) -> dict:
    return {name: {key: field[key] for key in ("label", "type", "hint", "required") if key in field}
            for name, field in spec["fields"].items()}


def extract_records(text: str, spec: dict) -> list[dict]:
    """Raw records from the LLM: [{field: {"value": ..., "quote": ...}}]."""
    excerpt = text[:settings.docs_max_text_chars]
    example = {name: {"value": None, "quote": None} for name in spec["fields"]}
    payload = {
        "document_type": spec.get("title"),
        "description": spec.get("description"),
        "multiple_records": bool(spec.get("multi")),
        "fields": _field_catalogue(spec),
        "answer_format": {"records": [example]},
        "document_text": excerpt,
    }
    messages = [{"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    try:
        answer = llm.parse_json_object(_llm_chat(messages))
    except ValueError as exc:
        logger.info("Extraction answer is not JSON: %s", exc)
        return []
    records = answer.get("records")
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        return []
    limit = 500 if spec.get("multi") else 1
    return [record for record in records if isinstance(record, dict)][:limit]


# --------------------------------------------------------------------------- checks
def check_record(raw: dict, spec: dict, normalized_text: str, known_wells: list[str],
                 today: date | None = None) -> dict:
    """Typed, quote-checked fields and whether the record can be loaded."""
    today = today or date.today()
    fields: dict[str, dict] = {}
    issues: list[str] = []
    for name, field in spec["fields"].items():
        item = raw.get(name)
        if not isinstance(item, dict):
            item = {"value": item, "quote": None}
        raw_value, quote = item.get("value"), item.get("quote")
        quote = str(quote).strip()[:MAX_QUOTE_CHARS] if quote not in (None, "") else None
        entry: dict[str, Any] = {"value": None, "quote": quote, "status": "missing"}
        try:
            value = parse_value(raw_value, field["type"])
        except ValueError:
            value = None
            entry["status"] = "invalid"
            entry["note"] = f"Значение «{str(raw_value)[:80]}» не распознано как {field['type']}."
        if value is not None:
            if not quote:
                entry["status"] = "no_quote"
                entry["note"] = "Модель не привела цитату — значение не принято."
            elif not quote_found(quote, normalized_text):
                entry["status"] = "quote_not_found"
                entry["note"] = "Цитата не найдена в тексте документа — значение не принято."
            elif not value_agrees(value, field["type"], quote, name):
                entry["status"] = "mismatch"
                entry["note"] = "Значение не совпадает с цитатой — не принято."
            else:
                entry["status"] = "ok"
                entry["value"] = value.isoformat() if isinstance(value, date) else value
        fields[name] = entry

    well = None
    if fields["well_id"]["status"] == "ok":
        well = match_well(fields["well_id"]["value"], known_wells)
        if well is None:
            issues.append(f"Скважина «{fields['well_id']['value']}» не найдена в справочнике.")
    for name, field in spec["fields"].items():
        if field.get("required") and fields[name]["status"] != "ok":
            issues.append(f"Не найдено обязательное поле «{field.get('label', name)}».")
        if field["type"] == "date" and fields[name]["status"] == "ok":
            value = date.fromisoformat(fields[name]["value"])
            if not EARLIEST_DATE <= value <= today + timedelta(days=1):
                fields[name]["status"] = "implausible"
                fields[name]["note"] = "Дата вне допустимого диапазона — не принята."
                fields[name]["value"] = None
                if field.get("required"):
                    issues.append(f"Неправдоподобная дата «{field.get('label', name)}»: {value:%d.%m.%Y}.")
    informative = spec.get("informative_fields") or []
    if informative and not any(fields[name]["status"] == "ok" for name in informative):
        labels = ", ".join(spec["fields"][name].get("label", name) for name in informative)
        issues.append(f"В документе нет данных по существу ({labels}).")
    return {"well_id": well, "fields": fields, "accepted": not issues, "issues": issues}


def values_of(record: dict) -> dict:
    """Accepted field values of a checked record."""
    return {name: entry["value"] for name, entry in record["fields"].items() if entry["status"] == "ok"}
