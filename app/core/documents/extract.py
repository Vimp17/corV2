"""Document type, field extraction by the LLM, and the checks that decide what is loaded.

The LLM (LiteLLM Chat Completions, MAI_DOCS_LLM_MODEL or MAI_LLM_MODEL) returns every value
with a quote copied from the document. A value is kept only when its quote is found in the
document text and agrees with the value; a record is accepted only when its required fields
survive, the well is known and the date is plausible. Nothing is guessed: a document whose
data cannot be confirmed loads nothing and its data counts as missing. A long text (after OCR)
goes to the model in parts of at most MAI_DOCS_CHUNK_CHARS characters (extract_records).
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
from app.core.documents.types import DATE_FIELD

logger = logging.getLogger(__name__)

EARLIEST_DATE = date(2000, 1, 1)
MAX_QUOTE_CHARS = 300
# Characters of the document start the classification reads.
CLASSIFY_CHARS = 6000
# The start of the document (title, well, date) given with every later part of a long text.
HEAD_CHARS = 800
# A part the model refuses as too long for its context window is halved, down to this size.
MIN_PART_CHARS = 500
_PAGE_START = re.compile(r"(?m)^(?==== Страница)")
_WELL_PREFIX = re.compile(r"^(?:скв(?:ажина)?\.?|well)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
# A space (also non-breaking or thin) between digit groups: «18 500».
_THOUSANDS = re.compile("(?<=\\d)[ \u00a0\u202f](?=\\d{3}\\b)")


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
    """Numbers written in the text. Table cells («a | b»), semicolons and tabs separate values,
    so «21000 | 125» is never read as 21000125; within a cell «18 500» may be one number
    (thousands separator) or two, so both readings are returned."""
    values: list[float] = []
    for cell in re.split(r"[|;\t]", str(text)):
        for variant in {cell, _THOUSANDS.sub("", cell)}:
            for item in _NUMBER.findall(variant):
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


def resolve_well(value: Any, known: list[str]) -> tuple[str | None, bool]:
    """(well, new): the known well the value names, or — when the system does not know it yet — the
    cleaned number as a new well (a document may be the first source of a well). None when the
    value has no digit and is not a well number."""
    well = match_well(value, known)
    if well is not None:
        return well, False
    cleaned = clean_well_id(value)
    if cleaned and re.search(r"\d", cleaned):
        return cleaned, True
    return None, False


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
        numbers = [float(item.replace(",", ".")) for item in _NUMBER.findall(_THOUSANDS.sub("", str(raw).strip()))]
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
        # Numbers are read from the quote as written: normalize() turns table pipes into spaces.
        for number in _numbers_in(str(quote)):
            if abs(number - value) < 1e-6 or (field_name == "inhibitor_efficiency" and "%" in quote
                                              and abs(number / 100 - value) < 1e-9):
                return True
        return False
    if field_type == "boolean":
        if field_name != "corrosion_detected" or re.search(r"возмож|не исключ|не подтвержд|проверить", text):
            return False
        negative = re.search(r"(?:коррози\w*[^.;]{0,20}(?:не обнаруж|не выяв|отсутств|нет)|"
                             r"(?:нет|без|отсутствие)\s+(?:признак\w*\s+)?коррози|"
                             r"не (?:обнаруж|выяв)\w*[^.;]{0,30}коррози)", text)
        if negative:
            return value is False
        positive = re.search(r"обнаруж|выяв|следы корроз|коррозионн\w* износ|язвенн|свищ", text)
        return value is True and bool(positive)
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
        if spec.get("fallback"):
            continue
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
    except llm.ContextTooLong:
        raise
    except llm.LLMError as exc:
        raise ExtractionUnavailable(f"Языковая модель недоступна: {exc}") from exc


def classify_by_llm(text: str, types: dict) -> str | None:
    catalogue = {type_id: f"{spec.get('title')}: {spec.get('description', '')}" for type_id, spec in types.items()
                 if not spec.get("fallback")}
    limit = min(CLASSIFY_CHARS, settings.docs_chunk_chars)
    while True:
        messages = [
            {"role": "system", "content": (
                "Определи тип документа нефтедобывающего предприятия по его тексту. Ответь только JSON "
                '{"type": "<код типа>"} с одним из кодов из списка или {"type": "other"}, если документ '
                "не относится ни к одному типу или текст нечитаем.")},
            {"role": "user", "content": json.dumps({"types": catalogue, "text": text[:limit]}, ensure_ascii=False)},
        ]
        try:
            answer = llm.parse_json_object(_llm_chat(messages))
            break
        except llm.ContextTooLong:
            if limit <= MIN_PART_CHARS:
                return None
            limit //= 2
        except ValueError:
            return None
    type_id = str(answer.get("type") or "").strip()
    return type_id if type_id in catalogue else None


# --------------------------------------------------------------------------- extraction
EXTRACTION_SYSTEM_PROMPT = (
    "Ты извлекаешь данные из документа нефтедобывающего предприятия (акт, протокол, паспорт, справка). "
    "Отвечай только JSON без пояснений. Правила:\n"
    "1. Бери значения только из текста документа. Ничего не додумывай и не вычисляй, кроме перевода "
    "единиц, прямо указанного в подсказке поля.\n"
    "2. Для каждого найденного значения укажи quote — короткую дословную цитату из текста (до 200 "
    "символов), где это значение записано; копируй символ в символ, без исправлений.\n"
    "3. Если значения в документе нет или оно нечитаемо — value: null и quote: null.\n"
    "4. Даты — в формате ГГГГ-ММ-ДД; числа — числом с точкой; логические поля — true, false или null.\n"
    "5. Если в документе несколько скважин или проб, верни отдельную запись на каждую.\n"
    "6. Ищи все поля из списка, а не только типичные для этого вида документа: например, тоннаж или "
    "дебит в протоколе анализа тоже нужно извлечь.\n"
    "7. Длинный документ приходит частями: извлекай данные из document_text. Если передано document_start "
    "(начало документа), номер скважины, дату и другие реквизиты, которых нет в части, бери оттуда с "
    "цитатой оттуда же; показатели из него не повторяй."
)


def _field_catalogue(spec: dict) -> dict:
    return {name: {key: field[key] for key in ("label", "type", "hint", "required") if key in field}
            for name, field in spec["fields"].items()}


def split_text(text: str, size: int) -> list[str]:
    """The text in parts of at most `size` characters: whole pages where they fit, else whole lines
    (a longer line is cut)."""
    pieces: list[str] = []
    for page in _PAGE_START.split(text):
        if len(page) <= size:
            pieces.append(page)
            continue
        for line in page.splitlines(keepends=True):
            pieces += [line[start:start + size] for start in range(0, len(line), size)]
    parts = [""]
    for piece in pieces:
        if parts[-1] and len(parts[-1]) + len(piece) > size:
            parts.append("")
        parts[-1] += piece
    return [part for part in parts if part.strip()]


def _first_part(text: str, size: int) -> str:
    """The start of the text: at most `size` characters, in whole lines."""
    parts = split_text(text, size)
    return parts[0] if parts else ""


def extract_records(text: str, spec: dict) -> list[dict]:
    """Raw records from the LLM: [{field: {"value": ..., "quote": ...}}].

    A text longer than MAI_DOCS_CHUNK_CHARS (a model with a small context window) is sent in parts,
    cut between pages, else between lines; every later part comes with the start of the document
    (its well and date). A part the model refuses as too long for its context window is halved, and
    so is the start sent with it. Each quote is checked against the whole text (check_record),
    whichever part it came from."""
    text = text[:settings.docs_max_text_chars]
    size = settings.docs_chunk_chars
    head = _first_part(text, min(HEAD_CHARS, size // 4))
    queue = [(part, head if index else "") for index, part in
             enumerate(split_text(text, size if len(text) <= size else size - len(head)))]
    records: list[dict] = []
    while queue:
        part, start = queue.pop(0)
        try:
            records += _extract_part(part, spec, start)
        except llm.ContextTooLong:
            if len(part) + len(start) <= MIN_PART_CHARS:
                raise ExtractionUnavailable("Текст не помещается в контекстное окно модели даже частями по "
                                            f"{MIN_PART_CHARS} символов.") from None
            shorter = _first_part(start or head, len(start or head) // 2 + 1)
            queue[:0] = [(half, shorter if start or index else "")
                         for index, half in enumerate(split_text(part, len(part) // 2 + 1))]
    return _merge_parts(records, spec)


def _extract_part(part: str, spec: dict, head: str) -> list[dict]:
    example = {name: ({"value": [{"name": "название, единица", "value": None, "quote": None}]}
                      if field["type"] == "measurements" else {"value": None, "quote": None})
               for name, field in spec["fields"].items()}
    payload = {
        "document_type": spec.get("title"),
        "description": spec.get("description"),
        "multiple_records": bool(spec.get("multi")),
        "fields": _field_catalogue(spec),
        "answer_format": {"records": [example]},
        **({"document_start": head} if head else {}),
        "document_text": part,
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
    return [record for record in records if isinstance(record, dict)][:None if spec.get("multi") else 1]


def _value(item: Any) -> Any:
    return item.get("value") if isinstance(item, dict) else item


def _merge_parts(records: list[dict], spec: dict) -> list[dict]:
    """The answers for the parts of a text as the records of the document. A single-record type is one
    record: each field from the first part that has it. A table type keeps its records, but a record
    with the well and date of an earlier one and no value against it (a row read twice, or split
    between parts) completes that one. Other indicators of merged records are joined."""
    keys = ("well_id", DATE_FIELD[spec["target"]])
    merged: list[dict] = []
    for record in records:
        target = next((other for other in merged if not spec.get("multi") or (
            all(_value(record.get(key)) not in (None, "") and _value(record.get(key)) == _value(other.get(key))
                for key in keys) and _agree(record, other, spec))), None)
        if target is None:
            merged.append(dict(record))
            continue
        for name, item in record.items():
            if spec["fields"].get(name, {}).get("type") == "measurements":
                found = [entry for entry in (_value(target.get(name)), _value(item)) if isinstance(entry, list)]
                target[name] = {"value": [entry for items in found for entry in items] or None}
            elif _value(target.get(name)) in (None, ""):
                target[name] = item
    return merged[:500]


def _agree(one: dict, other: dict, spec: dict) -> bool:
    """No field has two different values in the two records."""
    return all(_value(one.get(name)) in (None, "") or _value(other.get(name)) in (None, "")
               or _value(one.get(name)) == _value(other.get(name))
               for name, field in spec["fields"].items() if field["type"] != "measurements")


# --------------------------------------------------------------------------- checks
def check_record(raw: dict, spec: dict, normalized_text: str, known_wells: list[str],
                 today: date | None = None) -> dict:
    """Typed, quote-checked fields and whether the record can be loaded."""
    today = today or date.today()
    fields: dict[str, dict] = {}
    issues: list[str] = []
    for name, field in spec["fields"].items():
        item = raw.get(name)
        if field["type"] == "measurements":
            fields[name] = _check_measurements(item, normalized_text, {
                _stem(other.get("label", key)) for key, other in spec["fields"].items() if key != name})
            continue
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

    well, new_well = None, False
    if fields["well_id"]["status"] == "ok":
        well, new_well = resolve_well(fields["well_id"]["value"], known_wells)
        if well is None:
            issues.append(f"Не распознан номер скважины «{fields['well_id']['value']}».")
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
        labels = [spec["fields"][name].get("label", name) for name in informative]
        issues.append("В документе нет данных по существу (" + ", ".join(labels[:5])
                      + (f" и ещё {len(labels) - 5} полей карточки" if len(labels) > 5 else "") + ").")
    return {"well_id": well, "new_well": new_well, "fields": fields, "accepted": not issues, "issues": issues}


def _stem(label: str) -> str:
    """First word of a label, for spotting an "other" indicator that is one of the fields."""
    words = re.findall(r"\w+", normalize(label).replace("₂", "2"))
    return words[0] if words else ""


def _check_measurements(item: Any, normalized_text: str, field_stems: set[str]) -> dict:
    """Other indicators: each needs a name, a number and a quote that holds that number.
    Indicators that are fields of the document type (CO₂, хлориды…) are not repeated here."""
    items = item.get("value") if isinstance(item, dict) else item
    accepted, rejected, seen = [], 0, set()
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict):
            continue
        name = " ".join(str(entry.get("name") or "").split())[:100]
        quote = str(entry.get("quote") or "").strip()[:MAX_QUOTE_CHARS]
        try:
            value = parse_value(entry.get("value"), "number")
        except ValueError:
            value = None
        key = normalize(name)
        if (not name or value is None or not quote or key in seen or _stem(name) in field_stems
                or not quote_found(quote, normalized_text) or not value_agrees(value, "number", quote, name)):
            rejected += 1
            continue
        seen.add(key)
        accepted.append({"name": name, "value": value, "quote": quote})
    result: dict[str, Any] = {"value": accepted or None, "quote": None, "status": "ok" if accepted else "missing"}
    if rejected:
        result["note"] = f"Не подтверждено показателей: {rejected}."
    return result


def values_of(record: dict) -> dict:
    """Accepted field values of a checked record."""
    return {name: entry["value"] for name, entry in record["fields"].items() if entry["status"] == "ok"}
