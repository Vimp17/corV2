"""Reading measurements from an arbitrary Excel workbook without AI.

Used for Excel documents that are not a ДАРС / ХИМИК / ЭРА export. Each sheet is read as
  a table   - a header row (in the first 30 rows) with a well column; indicator columns are
              recognised by the source-profile aliases and by name patterns (CO₂, хлориды,
              обводнённость…); the date comes from a date column or from the sheet title
              («Дата отбора: 16.10.2026»);
  a form    - one sample per sheet: «Скважина № 1234», «Дата отбора 16.10.2026» and rows
              «Хлориды, мг/л | 21000», «pH | 6,8».
A record is accepted only with a well from the register, a date and at least one indicator.
Indicators without a system field (pH, hardness…) become additional indicators. Every value
keeps the sheet and cell it came from, shown on the dashboard in place of a quote.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from zoneinfo import ZoneInfo

from openpyxl.utils import get_column_letter

from app.config import settings
from app.core import excel_cells as cells
from app.core.excel_ingestion import load_profiles
from app.core.documents.extract import EARLIEST_DATE, resolve_well

MAX_ROWS = 2000
MAX_COLUMNS = 60
HEADER_SCAN_ROWS = 30
MAX_REJECTED_STORED = 50

FIELD_LABELS = {field: label for field, _, label in cells.FIELD_PATTERNS} | {
    "well_id": "Скважина", "sample_date": "Дата пробы / замера", "field": "Месторождение",
    "extra_measurements": "Другие показатели",
}
WELL_LABEL = re.compile(r"скважин|(^|[^a-zа-я])скв([^a-zа-я]|$)|^well", re.IGNORECASE)
WELL_INLINE = re.compile(r"(?:скважин[аыуе]?|скв\.?)\s*(?:№|n|#|no\.?)?\s*:?\s*([A-Za-zА-Яа-яЁё0-9][\w\-/.]*)",
                         re.IGNORECASE)
DATE_LABEL = re.compile(r"дата|date|^число$", re.IGNORECASE)
# The date of a sample is preferred to the date of the protocol.
DATE_PRIORITY = ("отбор", "проб", "анализ", "замер", "измерен", "протокол", "")
IDENTIFIER = re.compile(r"(^|[^a-zа-я])(№\s*п/?п|п/?п|куст|цех|участок|код|пласт|бригад|примечан|номер|№\s*пробы|"
                        r"протокол|лаборатор|лаборант|исполнит|подпис|телефон|итого|всего)", re.IGNORECASE)


def _alias_map() -> dict[str, str]:
    """Header aliases of the telemetry source profiles: normalised header -> field."""
    aliases: dict[str, str] = {}
    try:
        profiles = load_profiles()
    except Exception:
        return aliases
    for profile in profiles.values():
        if profile.get("kind") != "telemetry":
            continue
        for field, names in (profile.get("field_map") or {}).items():
            for name in [field, *names]:
                aliases.setdefault(cells.header_key(name), field)
    return aliases


def _entry(value: Any, quote: str) -> dict:
    return {"value": value, "quote": quote, "status": "ok"}


class _Sheet:
    def __init__(self, title: str, rows: list[list[Any]]) -> None:
        self.title = title
        self.rows = rows

    def texts(self, limit: int | None = None):
        for r, row in enumerate(self.rows if limit is None else self.rows[:limit], start=1):
            for c, value in enumerate(row):
                if isinstance(value, str) and value.strip():
                    yield r, c, value.strip()

    def right_of(self, r: int, c: int) -> tuple[int, Any] | None:
        row = self.rows[r - 1]
        for index in range(c + 1, len(row)):
            if row[index] is not None and str(row[index]).strip():
                return index, row[index]
        return None


def _sheet_date(sheet: _Sheet, limit: int | None = None) -> tuple[datetime, str] | None:
    """The date of the sheet: «Дата отбора: 16.10.2026» or a «Дата …» label with a date beside it."""
    found: list[tuple[int, datetime, str]] = []
    for r, c, text in sheet.texts(limit):
        if not DATE_LABEL.search(text):
            continue
        low = cells.norm(text)
        rank = next(index for index, word in enumerate(DATE_PRIORITY) if word in low)
        moment = cells.moment(text)
        quote = f"Лист «{sheet.title}», {get_column_letter(c + 1)}{r}: {text}"[:300]
        if moment is None:
            beside = sheet.right_of(r, c)
            if beside is not None:
                moment = cells.moment(beside[1])
                quote = cells.cell_ref(sheet.title, r, beside[0], text, beside[1])
        if moment is not None:
            found.append((rank, moment, quote))
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[0][1], found[0][2]


def _header_row(sheet: _Sheet, aliases: dict[str, str]) -> tuple[int, int, int | None, dict[int, str], dict[int, str]] | None:
    """(row, well column, date column, field columns, other columns) of a table, or None."""
    for r, row in enumerate(sheet.rows[:HEADER_SCAN_ROWS], start=1):
        labels = {c: str(value).strip() for c, value in enumerate(row) if isinstance(value, str) and value.strip()}
        well_column = next((c for c, text in labels.items()
                            if WELL_LABEL.search(cells.norm(text)) and not re.search(r"\d", text) and len(text) <= 40), None)
        if well_column is None:
            continue
        date_column = next((c for c, text in labels.items() if c != well_column and DATE_LABEL.search(cells.norm(text))), None)
        fields, others = {}, {}
        for c, text in labels.items():
            if c in {well_column, date_column}:
                continue
            field = cells.field_for(text, aliases)
            if field and field not in fields.values():
                fields[c] = field
            elif not IDENTIFIER.search(cells.norm(text)):
                others[c] = text
        if fields or others:
            return r, well_column, date_column, fields, others
    return None


def _record(well_raw: str, well_quote: str, moment: datetime | None, date_quote: str | None,
            values: dict[str, tuple[Any, str]], extras: list[dict], known: list[str]) -> dict:
    if moment is not None:
        # The day as written in the file (source time zone), not the UTC day of local midnight.
        moment = moment.astimezone(ZoneInfo(settings.source_timezone))
    fields: dict[str, dict] = {"well_id": _entry(well_raw, well_quote)}
    issues: list[str] = []
    well, new_well = resolve_well(well_raw, known)
    if well is None:
        issues.append(f"Не распознан номер скважины «{well_raw}».")
    if moment is not None and not EARLIEST_DATE <= moment.date() <= date.today() + timedelta(days=1):
        issues.append(f"Неправдоподобная дата {moment:%d.%m.%Y}.")
        moment = None
        fields["sample_date"] = {"value": None, "quote": date_quote, "status": "implausible"}
    elif moment is None:
        issues.append("Не найдена дата (столбец «Дата» или «Дата отбора: …» в шапке).")
        fields["sample_date"] = {"value": None, "quote": None, "status": "missing"}
    else:
        fields["sample_date"] = _entry(moment.date().isoformat(), date_quote)
    for field, (value, quote) in values.items():
        fields[field] = _entry(value, quote)
    fields["extra_measurements"] = ({"value": extras, "quote": None, "status": "ok"} if extras
                                    else {"value": None, "quote": None, "status": "missing"})
    if not values and not extras:
        issues.append("Нет показателей.")
    return {"well_id": well, "new_well": new_well, "fields": fields, "accepted": not issues, "issues": issues,
            "moment": moment.isoformat() if moment else None}


def _read_table(sheet: _Sheet, header: tuple, aliases: dict[str, str], known: list[str]) -> list[dict]:
    header_row, well_column, date_column, fields, others = header
    titles = {c: str(v).strip() for c, v in enumerate(sheet.rows[header_row - 1]) if v is not None}
    sheet_date = _sheet_date(sheet, header_row - 1) if date_column is None else None
    records, empty = [], 0
    for r in range(header_row + 1, len(sheet.rows) + 1):
        row = sheet.rows[r - 1]
        well_raw = cells.well_text(row[well_column]) if well_column < len(row) else ""
        if not well_raw:
            empty += 1
            if empty > 20:
                break
            continue
        empty = 0
        if IDENTIFIER.search(cells.norm(well_raw)) or cells.norm(well_raw) in {"итого", "всего"}:
            continue
        moment, date_quote = None, None
        if date_column is not None and date_column < len(row):
            moment = cells.moment(row[date_column])
            date_quote = cells.cell_ref(sheet.title, r, date_column, titles.get(date_column, "Дата"), row[date_column])
        elif sheet_date is not None:
            moment, date_quote = sheet_date
        values: dict[str, tuple[Any, str]] = {}
        for c, field in fields.items():
            raw = row[c] if c < len(row) else None
            if field == "field":
                if isinstance(raw, str) and raw.strip():
                    values[field] = (raw.strip()[:256], cells.cell_ref(sheet.title, r, c, titles[c], raw))
                continue
            number = cells.number(raw)
            if number is not None:
                values[field] = (cells.adjust(field, titles[c], number), cells.cell_ref(sheet.title, r, c, titles[c], raw))
        extras = []
        for c, name in others.items():
            number = cells.number(row[c] if c < len(row) else None)
            if number is not None:
                extras.append({"name": " ".join(name.split())[:100], "value": number,
                               "quote": cells.cell_ref(sheet.title, r, c, name, row[c])})
        records.append(_record(well_raw, cells.cell_ref(sheet.title, r, well_column, titles[well_column], row[well_column]),
                               moment, date_quote, values, extras, known))
    return records


def _read_form(sheet: _Sheet, aliases: dict[str, str], known: list[str]) -> list[dict]:
    """One sample per sheet: the well, the date and «indicator | value» rows."""
    well_raw, well_quote = None, None
    for r, c, text in sheet.texts():
        match = WELL_INLINE.search(text)
        if match and re.search(r"\d", match.group(1)):
            well_raw, well_quote = match.group(1).rstrip(".,;"), cells.cell_ref(sheet.title, r, c, "Скважина", text)
            break
        if WELL_LABEL.search(cells.norm(text)) and not re.search(r"\d", text):
            beside = sheet.right_of(r, c)
            if beside is not None and re.search(r"\d", cells.well_text(beside[1])):
                well_raw, well_quote = cells.well_text(beside[1]), cells.cell_ref(sheet.title, r, beside[0], text, beside[1])
                break
    if well_raw is None:
        return []
    sheet_date = _sheet_date(sheet)
    values: dict[str, tuple[Any, str]] = {}
    extras: list[dict] = []
    for r, row in enumerate(sheet.rows, start=1):
        label_index = next((c for c, value in enumerate(row) if isinstance(value, str) and re.search(r"[A-Za-zА-Яа-яЁё]", value)), None)
        if label_index is None:
            continue
        label = str(row[label_index]).strip()
        low = cells.norm(label)
        if (len(label) > 80 or WELL_LABEL.search(low) or DATE_LABEL.search(low) or IDENTIFIER.search(low)
                or WELL_INLINE.search(label)):
            continue
        unit, number, column = None, None, None
        for c in range(label_index + 1, len(row)):
            candidate = row[c]
            if candidate is None or not str(candidate).strip():
                continue
            number = cells.number(candidate)
            if number is not None:
                column = c
                break
            if unit is None and isinstance(candidate, str) and len(candidate) <= 15:
                unit = candidate.strip()
            else:
                break
        if number is None:
            continue
        name = f"{label}, {unit}" if unit and unit.casefold() not in low else label
        field = cells.field_for(name, aliases)
        quote = cells.cell_ref(sheet.title, r, column, name, row[column])
        if field == "field":
            continue
        if field and field not in values:
            values[field] = (cells.adjust(field, name, number), quote)
        elif not field:
            extras.append({"name": " ".join(name.split())[:100], "value": number, "quote": quote})
    moment, date_quote = sheet_date if sheet_date else (None, None)
    return [_record(well_raw, well_quote, moment, date_quote, values, extras, known)]


def read_workbook(content: bytes, known_wells: list[str]) -> dict:
    """Records found in the workbook (accepted and not) and a summary of what was found."""
    aliases = _alias_map()
    records: list[dict] = []
    layouts: list[str] = []
    for title, rows in cells.sheet_rows(content, MAX_ROWS, MAX_COLUMNS):
        sheet = _Sheet(title, rows)
        header = _header_row(sheet, aliases)
        if header is not None:
            found = _read_table(sheet, header, aliases, known_wells)
            if found:
                layouts.append("table")
                records.extend(found)
                continue
        found = _read_form(sheet, aliases, known_wells)
        if found:
            layouts.append("form")
            records.extend(found)
    accepted = [record for record in records if record["accepted"]]
    rejected = [record for record in records if not record["accepted"]]
    return {"records": accepted + rejected[:MAX_REJECTED_STORED], "accepted": len(accepted), "rejected": len(rejected),
            "layout": "form" if layouts and set(layouts) == {"form"} else "table" if layouts else None,
            "field_labels": FIELD_LABELS}


def not_found_reason(result: dict) -> str:
    """Why nothing was loaded, in words for the user."""
    records = result["records"]
    if not records:
        return ("В файле не найдена скважина: нужен столбец «Скважина» (или «№ скв.») в таблице "
                "или строка «Скважина № …» в форме.")
    issues = list(dict.fromkeys(issue for record in records for issue in record["issues"]))
    return "Данные не загружены: " + " ".join(issues[:4])
