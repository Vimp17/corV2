"""Cells of Excel tables, read the same way by every reader: the source exports (app.core.excel_ingestion),
other tables and forms (app.core.documents.excel_reader) and the НКТ registry (app.core.documents.
nkt_registry). Column names, numbers, dates, well numbers, the indicator a column or row name stands
for, and the cell a value came from."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.config import settings

# The liquid rate: «дебит жидкости», «средний дебит по жидкости», «Qж», a bare «Дебит, м³/сут» —
# not «дебит нефти» or «дебит газа».
LIQUID_RATE = r"дебит\w*\s+(по\s+)?жидк|(^|[^a-zа-я])qж|^(средн\w*\s+)?(суточн\w*\s+)?дебит\w*\s*(,|\(|$)"
# Recognised indicators: (field, name pattern, label). Specific patterns go first.
FIELD_PATTERNS: list[tuple[str, str, str]] = [
    ("initial_wall_thickness_mm", r"(начальн|исходн)\w*\s+толщин", "Начальная толщина стенки, мм"),
    ("wall_thickness_mm", r"толщин\w*(\s+стенк)?", "Толщина стенки, мм"),
    ("metal_loss_mm", r"потер\w*\s+металл", "Потеря металла, мм"),
    ("corrosion_rate_mm_year", r"скорост\w*\s+коррози", "Скорость коррозии, мм/год"),
    ("inhibitor_efficiency", r"эффективн\w*\s+(ингибитор|защит)", "Эффективность ингибитора, доля"),
    ("injection_deviation_pct", r"отклонен\w*\s+(закачк|дозировк|подач)", "Отклонение закачки, %"),
    ("water_cut_pct", r"обводн", "Обводнённость, %"),
    ("chlorides_mg_l", r"хлорид|(^|[^a-zа-я])cl([^a-zа-я]|$)", "Хлориды, мг/л"),
    ("co2_pct", r"(^|[^a-zа-я0-9])(co2|со2|co₂|со₂)([^a-zа-я0-9]|$)|углекисл|диоксид\w*\s+углерод", "CO₂, %"),
    ("liquid_rate_m3_day", LIQUID_RATE, "Дебит жидкости, м³/сут"),
    ("tonnage_t", r"тоннаж", "Тоннаж, т"),
    ("noo_days", r"(^|[^a-zа-я])ноо([^a-zа-я]|$)", "НОО, сут"),
]
FIELD_LABEL = re.compile(r"месторожден", re.IGNORECASE)
DATE_TEXT = re.compile(r"(\d{1,2}\.\d{1,2}\.\d{2,4}(?:\s+\d{1,2}:\d{2})?|\d{4}-\d{2}-\d{2})")
_NUMBER = re.compile(r"[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?")


def sheet_rows(content: bytes, max_rows: int, max_columns: int) -> list[tuple[str, list[list[Any]]]]:
    """(title, rows of cell values) of every sheet of the workbook, within the limits."""
    workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
    try:
        return [(sheet.title, [list(values)[:max_columns] for values in sheet.iter_rows(max_row=max_rows, values_only=True)])
                for sheet in workbook.worksheets]
    finally:
        workbook.close()


def header_key(value: Any) -> str:
    """A column name compared ignoring case, spaces and punctuation."""
    return "".join(char for char in str(value or "").strip().casefold() if char.isalnum())


def norm(text: Any) -> str:
    """Text for matching names: ё → е, single spaces, lower case."""
    return " ".join(str(text or "").replace("ё", "е").replace("Ё", "Е").split()).casefold()


def number(value: Any) -> float | None:
    """A cell as a number: a number as is (not NaN or infinite), or text with a decimal comma and spaces
    between thousands («18 500,5»); None for an empty cell, a date, yes/no or other text («н/д»)."""
    if value is None or isinstance(value, (bool, datetime, date)):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value and abs(value) != float("inf") else None
    text = str(value).strip()
    for space in (" ", " ", " "):
        text = text.replace(space, "")
    text = text.replace(",", ".")
    return float(text) if _NUMBER.fullmatch(text) else None


def as_datetime(value: Any) -> datetime:
    """An Excel date, ISO 8601 or «16.10.2026 10:30» as UTC; a time without a zone is in
    MAI_SOURCE_TIMEZONE. Raises ValueError for anything else."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("date is empty")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
            # Dates typed as text the Russian way: 16.10.2026, 16.10.2026 10:30, 16.10.26.
            for pattern in ("%d.%m.%Y", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%y", "%d.%m.%y %H:%M"):
                try:
                    parsed = datetime.strptime(" ".join(raw.split()), pattern)
                    break
                except ValueError:
                    continue
            if parsed is None:
                raise ValueError("date must be an Excel date, ISO 8601 or ДД.ММ.ГГГГ value") from None
    else:
        raise ValueError("date must be an Excel date or ISO 8601 value")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(settings.source_timezone))
    return parsed.astimezone(timezone.utc)


def moment(value: Any) -> datetime | None:
    """A date in a cell, also inside text («Дата отбора: 16.10.2026»); None when there is none."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        match = DATE_TEXT.search(value)
        if not match:
            return None
        value = match.group(1)
    try:
        return as_datetime(value)
    except (TypeError, ValueError):
        return None


def well_text(value: Any) -> str:
    """A well number as written: 1234.0 read from a number cell is «1234»."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value or "").strip()


def adjust(field: str, label: str, value: float) -> float:
    """Inhibitor efficiency in percent («85 %») is stored as a fraction."""
    if field == "inhibitor_efficiency" and (value > 1.5 or "%" in label):
        return round(value / 100, 4)
    return value


def field_for(label: Any, aliases: dict[str, str]) -> str | None:
    """System field of a column or row name — by the source-profile aliases, else by the name
    patterns — or None for an unknown indicator."""
    text = norm(label)
    if not text:
        return None
    field = aliases.get(header_key(text))
    if field in {"timestamp", "well_id", "in_operation", "water_compatibility_issue"}:
        return None
    if field:
        return field
    if FIELD_LABEL.search(text):
        return "field"
    for name, pattern, _label in FIELD_PATTERNS:
        if re.search(pattern, text):
            return name
    return None


def cell_ref(sheet: str, row: int, column: int, label: str, value: Any) -> str:
    """Where a value came from, shown in place of a quote: «Лист «Пробы», D7: Хлориды, мг/л = 21000»."""
    shown = value.strftime("%d.%m.%Y") if isinstance(value, (datetime, date)) else value
    return f"Лист «{sheet}», {get_column_letter(column + 1)}{row}: {label} = {shown}"[:300]
