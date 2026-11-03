"""Excel export of the computed well rating, in the approved layout.

Sheets: «Рейтинг» (one row per well, sorted by rank), «График ППР» (12 weeks from the Monday of
the export date), «Сводка», «Источники данных» (where each value came from) and «Параметры»
(the thresholds and deadlines the backend used). Every value is what GET /api/v1/rating returns,
written as a value rather than a formula, so the file matches the dashboard exactly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.core import rating

FONT = "Arial"
NAVY, NAVY2, BLUE = "0E2A47", "16375C", "0072CE"
F_BASE = Font(name=FONT, size=10)
F_BOLD = Font(name=FONT, size=10, bold=True)
F_TITLE = Font(name=FONT, size=14, bold=True, color=NAVY)
F_NOTE = Font(name=FONT, size=9, italic=True, color="5A6B7C")
F_HEAD = Font(name=FONT, size=10, bold=True, color="FFFFFF")
F_BAD = Font(name=FONT, size=10, bold=True, color="B71C1C")
FILL_HEAD = PatternFill("solid", fgColor=NAVY)
FILL_GROUP = PatternFill("solid", fgColor=NAVY2)
_THIN = Side(style="thin", color="DDE3EA")
BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)

# Background / text colour per measure (same palette as the dashboard).
MEASURE_COLORS = {
    rating.MEASURE_REPAIR: ("FDECEA", "B71C1C"),
    rating.MEASURE_ENGINEER: ("FDEFE3", "BF4300"),
    rating.MEASURE_UD: ("FFF4D6", "8A5300"),
    rating.MEASURE_NONE: ("E8F5E9", "2E7D32"),
}

COLUMNS = [
    # key, header, width, group
    ("rank", "Место", 7, "Скважина"),
    ("well_id", "№ скважины", 11, "Скважина"),
    ("field", "Месторождение", 16, "Скважина"),
    ("equipment", "Спущенное оборудование", 22, "Скважина"),
    ("in_operation", "Статус", 12, "Скважина"),
    ("run_days", "Наработка, сут", 10, "Скважина"),
    ("liquid_rate_m3_day", "Жидкость, м³/сут", 10, "Скважина"),
    ("oil_l_day", "Нефть, л/сут", 10, "Скважина"),
    ("oil_pct", "Нефть, %", 8, "Скважина"),
    ("sampling", "Отбор проб", 10, "Среда и обработка"),
    ("corrosive_environment", "Среда", 15, "Среда и обработка"),
    ("tonnage_t", "Тоннаж, т", 10, "Среда и обработка"),
    ("noo_days", "НОО, сут", 10, "Среда и обработка"),
    ("demount_act", "Акт демонтажа", 11, "Демонтаж и разбор (исторические данные)"),
    ("demount_corrosion", "Признак коррозии", 11, "Демонтаж и разбор (исторические данные)"),
    ("base_act", "Акт разбора на базе", 11, "Демонтаж и разбор (исторические данные)"),
    ("base_corrosion", "Результат разбора", 14, "Демонтаж и разбор (исторические данные)"),
    ("category", "Категория скважины", 21, "Оценка"),
    ("protection", "Защита", 14, "Оценка"),
    ("model_ntc", "Прогноз НТЦ (0–1)", 10, "Оценка"),
    ("model_math", "Мат. модель (0–1)", 10, "Оценка"),
    ("index_raw", "Индекс без учёта защиты", 11, "Оценка"),
    ("index", "Индекс коррозии на НКТ (0–100)", 11, "Оценка"),
    ("measure", "Мероприятие", 26, "Решение и график ППР"),
    ("action", "Предложение", 44, "Решение и график ППР"),
    ("era", "ЭРА ремонта", 11, "Решение и график ППР"),
    ("promine_status", "Документ Промайн", 16, "Решение и график ППР"),
    ("deadline", "Крайний срок", 12, "Решение и график ППР"),
    ("plan_date", "План ППР (ДДО)", 12, "Решение и график ППР"),
    ("plan_status", "Статус плана", 15, "Решение и график ППР"),
    ("data_gaps", "Полнота данных", 30, ""),
    ("comment", "Комментарий", 34, ""),
]
FIELD_LABELS = {key: header for key, header, *_ in COLUMNS}


# Number formats and left-aligned (text) columns of «Рейтинг»; the rest are centred.
NUMBER_FORMATS = {"model_ntc": "0.00", "model_math": "0.00", "tonnage_t": "0.0", "liquid_rate_m3_day": "0.0", "oil_pct": "0.0",
                  "noo_days": "0", "index": "0", "index_raw": "0", "oil_l_day": "0",
                  "deadline": "DD.MM.YYYY", "plan_date": "DD.MM.YYYY"}
TEXT_COLUMNS = {"field", "equipment", "measure", "comment", "category", "action", "data_gaps"}
# Values shown in red bold on «Рейтинг».
BAD_VALUES = {"corrosive_environment": "Коррозионная", "demount_corrosion": "Есть коррозия",
              "base_corrosion": "Есть коррозия", "category": "Коррозионная", "protection": "Недостаточна"}


def _yn(value: Any, yes: str, no: str) -> str:
    return "—" if value is None else (yes if value else no)


def _date(value: Any) -> date | None:
    if not value:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _fill(color: str) -> PatternFill:
    return PatternFill("solid", fgColor=color)


def _put(sheet, row: int, column: int, value: Any = None, font: Font = F_BASE, align: Alignment | None = CENTER,
         fill: PatternFill | None = None, number_format: str | None = None):
    """A bordered cell with its value and style."""
    cell = sheet.cell(row, column, value)
    cell.font, cell.border = font, BORDER
    if align is not None:
        cell.alignment = align
    if fill is not None:
        cell.fill = fill
    if number_format:
        cell.number_format = number_format
    return cell


def _title(sheet, title: str, note: str | None = None) -> None:
    sheet["A1"] = title
    sheet["A1"].font = F_TITLE
    if note:
        sheet["A2"] = note
        sheet["A2"].font = F_NOTE


def _cell_values(well: dict) -> dict[str, Any]:
    """The «Рейтинг» row of a well: values as they are, yes/no and dates in words."""
    measure, era = well.get("measure"), well.get("era_required")
    return {key: well.get(key) for key, *_ in COLUMNS} | {
        "in_operation": _yn(well.get("in_operation"), "В работе", "Не в работе"),
        "data_gaps": "\n".join(f"{rating.GAP_LABELS.get(gap['key'], gap['key'])}: {gap.get('note') or ''}"
                               for gap in well.get("data_gaps") or []) or "полные",
        "sampling": _yn(well.get("sampling"), "Да", "Нет"),
        "corrosive_environment": _yn(well.get("corrosive_environment"), "Коррозионная", "Некоррозионная"),
        "demount_act": _yn(well.get("demount_act"), "Есть", "Нет"),
        "demount_corrosion": _yn(well.get("demount_corrosion"), "Есть коррозия", "Коррозии нет") if well.get("demount_act") else "—",
        "base_act": _yn(well.get("base_act"), "Есть", "Нет"),
        "base_corrosion": _yn(well.get("base_corrosion"), "Есть коррозия", "Коррозии нет") if well.get("base_act") else "—",
        "category": well.get("category_label") or "—",
        "protection": well.get("protection_label") or "Нет данных",
        "measure": well.get("measure_label") if measure else "Нет оценки",
        "era": "Включить" if era else "—",
        "promine_status": well.get("promine_status_label") or ("Не сформирован" if era else "—"),
        "deadline": _date(well.get("deadline")),
        "plan_date": _date(well.get("plan_date")),
        "plan_status": well.get("plan_status_label") or "—",
    }


def _head(sheet, row: int, labels: list[str]) -> None:
    for column, label in enumerate(labels, start=1):
        _put(sheet, row, column, label, F_HEAD, fill=FILL_HEAD)


def _rating_sheet(book: Workbook, wells: list[dict], generated: datetime) -> None:
    sheet = book.active
    sheet.title = "Рейтинг"
    _title(sheet, "Рейтинг скважин по риску коррозии",
           f"Выгрузка из системы «Контроль коррозии скважин» {generated:%d.%m.%Y %H:%M} UTC. "
           "Значения рассчитаны по данным ДАРС, ХИМИК и ЭРА ремонта; источники — на листе «Источники данных».")
    keys = [key for key, *_ in COLUMNS]
    first = 1
    for column, (_key, _header, width, group) in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width
        if column < len(COLUMNS) and COLUMNS[column][3] == group:
            continue
        for index in range(first, column + 1):     # the group title over its columns
            _put(sheet, 3, index, group if index == first else None, F_HEAD, fill=FILL_GROUP)
        if column > first:
            sheet.merge_cells(start_row=3, start_column=first, end_row=3, end_column=column)
        first = column + 1
    _head(sheet, 4, [header for _key, header, *_ in COLUMNS])
    sheet.row_dimensions[4].height = 44
    for row, well in enumerate(wells, start=5):
        values = _cell_values(well)
        colors = MEASURE_COLORS.get(well.get("measure"))
        for column, key in enumerate(keys, start=1):
            cell = _put(sheet, row, column, values[key], align=LEFT if key in TEXT_COLUMNS else CENTER,
                        number_format=NUMBER_FORMATS.get(key))
            if key in {"index", "measure", "era"} and colors:
                cell.fill = _fill(colors[0])
                cell.font = Font(name=FONT, size=10, bold=key != "era", color=colors[1])
            if key in BAD_VALUES and values[key] == BAD_VALUES[key]:
                cell.font = F_BAD
        status = sheet.cell(row, keys.index("plan_status") + 1)
        if well.get("plan_status") == "late":
            status.fill, status.font = _fill("FDECEA"), F_BAD
        elif well.get("plan_status") == "not_planned":
            status.fill, status.font = _fill("FFF4D6"), Font(name=FONT, size=10, bold=True, color="8A5300")
    sheet.freeze_panes = "C5"
    sheet.auto_filter.ref = f"A4:{get_column_letter(len(COLUMNS))}{4 + max(1, len(wells))}"
    sheet.sheet_view.zoomScale = 90
    if not wells:
        sheet["A5"] = "Рейтинг пуст: загрузите данные ДАРС, ХИМИК, ЭРА ремонта и выполните дневной цикл."
        sheet["A5"].font = F_NOTE


def _schedule_sheet(book: Workbook, wells: list[dict], today: date) -> None:
    sheet = book.create_sheet("График ППР")
    _title(sheet, "График ППР (ДДО) — мероприятия по рейтингу на 12 недель",
           "Код мероприятия стоит в неделе плановой даты; светлая заливка — окно до крайнего срока; «!» — план "
           "позже срока. Скважины без мероприятий не показаны.")
    heads = ["Место", "№ скважины", "Месторождение", "Индекс коррозии на НКТ", "Код", "Крайний срок", "План (ДДО)"]
    start = today - timedelta(days=today.weekday())
    weeks = [start + timedelta(days=7 * k) for k in range(12)]
    _head(sheet, 4, heads + [f"{week:%d.%m}" for week in weeks])
    for column, width in enumerate((7, 11, 16, 8, 7, 12, 12), start=1):
        sheet.cell(3, column).fill = FILL_HEAD
        sheet.column_dimensions[get_column_letter(column)].width = width
    for k, week in enumerate(weeks, start=len(heads) + 1):
        _put(sheet, 3, k, f"Нед. {k - len(heads)}", F_HEAD, fill=FILL_GROUP)
        if week <= today < week + timedelta(days=7):
            sheet.cell(4, k).fill = _fill(BLUE)
        sheet.column_dimensions[get_column_letter(k)].width = 8.5
    sheet.row_dimensions[4].height = 30
    planned = [well for well in wells if well.get("measure") not in (None, rating.MEASURE_NONE)]
    for row, well in enumerate(planned, start=5):
        deadline, plan = _date(well.get("deadline")), _date(well.get("plan_date"))
        code = well.get("measure_code") or ""
        for column, value in enumerate([well.get("rank"), well["well_id"], well.get("field"), well.get("index"), code,
                                        deadline, plan], start=1):
            _put(sheet, row, column, value, align=LEFT if column == 3 else CENTER,
                 number_format="DD.MM.YYYY" if column >= 6 else None)
        background, foreground = MEASURE_COLORS[well["measure"]]
        for k, week in enumerate(weeks, start=len(heads) + 1):
            cell = sheet.cell(row, k)
            cell.border, cell.alignment = BORDER, CENTER
            week_end = week + timedelta(days=6)
            if plan and week <= plan <= week_end:
                late = deadline is not None and plan > deadline
                cell.value = code + (" !" if late else "")
                cell.fill = _fill("1A2430" if late else foreground)
                cell.font = Font(name=FONT, size=9, bold=True, color="FFFFFF")
            elif deadline and week_end >= today and week <= deadline:
                cell.fill = _fill(background)
    if not planned:
        sheet["A5"] = "Мероприятий по рейтингу нет."
        sheet["A5"].font = F_NOTE
    sheet.freeze_panes = sheet.cell(5, len(heads) + 1)
    sheet.sheet_view.zoomScale = 90


def _summary_sheet(book: Workbook, wells: list[dict], counts: dict) -> None:
    sheet = book.create_sheet("Сводка")
    _title(sheet, "Сводка по рейтингу")

    def rows(start: int, title: list[str], data: list[list]) -> int:
        _head(sheet, start, title)
        for row, values in enumerate(data, start=start + 1):
            for column, value in enumerate(values, start=1):
                _put(sheet, row, column, value, F_BASE if column == 1 else F_BOLD, LEFT if column == 1 else CENTER)
        return start + len(data) + 2

    measures = [rating.MEASURE_REPAIR, rating.MEASURE_ENGINEER, rating.MEASURE_UD, rating.MEASURE_NONE]
    nxt = rows(3, ["Мероприятие", "Скважин"],
               [[rating.MEASURE_LABELS[key], counts.get(key, 0)] for key in measures]
               + [["Нет оценок моделей", counts.get("unscored", 0)], ["Всего в рейтинге", counts.get("total", 0)]])
    nxt = rows(nxt, ["Контроль исполнения", "Скважин"], [
        ["Требуют включения в ЭРА ремонта", counts.get(rating.MEASURE_REPAIR, 0)],
        ["…из них документ Промайн ещё не направлен", counts.get("promine_pending", 0)],
        ["План ППР позже крайнего срока", counts.get("plan_late", 0)],
        ["Мероприятие назначено, но не запланировано", counts.get("plan_missing", 0)],
    ])
    fields = sorted({well.get("field") or "не указано" for well in wells})
    in_field = {name: [well for well in wells if (well.get("field") or "не указано") == name] for name in fields}
    nxt = rows(nxt, ["Месторождение", "ППР", "РИ", "УДР", "Всего"], [
        [name] + [sum(1 for well in in_field[name] if well.get("measure") == key) for key in measures[:3]]
        + [len(in_field[name])] for name in fields])
    rows(nxt, ["Категория скважины", "Скважин"], [
        [label, sum(1 for well in wells if well.get("category") == key)] for key, label in rating.CATEGORY_LABELS.items()])
    sheet.column_dimensions["A"].width = 46
    for letter in "BCDE":
        sheet.column_dimensions[letter].width = 14


def _sources_sheet(book: Workbook, wells: list[dict]) -> None:
    sheet = book.create_sheet("Источники данных")
    _title(sheet, "Откуда взяты значения рейтинга")
    _head(sheet, 3, ["№ скважины", "Поле", "Источник", "Дата данных", "Пояснение"])
    row = 4
    for well in wells:
        for key, item in (well.get("provenance") or {}).items():
            if not isinstance(item, dict):
                continue
            when = item.get("date")
            for column, value in enumerate([well["well_id"], FIELD_LABELS.get(key, key),
                                            item.get("source") or "расчёт системы",
                                            datetime.fromisoformat(when).replace(tzinfo=None) if when else None,
                                            item.get("note")], start=1):
                _put(sheet, row, column, value, align=LEFT, number_format="DD.MM.YYYY HH:MM" if column == 4 else None)
            row += 1
    for letter, width in zip("ABCDE", (12, 24, 22, 17, 80)):
        sheet.column_dimensions[letter].width = width
    sheet.freeze_panes = "A4"


def _parameters_sheet(book: Workbook, payload: dict) -> None:
    sheet = book.create_sheet("Параметры")
    _title(sheet, "Параметры рейтинга (настройки системы на момент выгрузки)")
    settings_block = payload.get("settings", {})
    _head(sheet, 3, ["Мероприятие", "Код", "Индекс от", "Индекс до", "Срок, дней", "ЭРА ремонта"])
    for row, item in enumerate(settings_block.get("measures", []), start=4):
        for column, value in enumerate([item["label"], item["code"], item["index_from"], item["index_to"],
                                        item.get("deadline_days"), "Включить" if item.get("era_required") else "—"],
                                       start=1):
            _put(sheet, row, column, value, align=LEFT if column == 1 else CENTER)
    protection = settings_block.get("protection") or {}
    models = settings_block.get("models") or {}
    combined = "среднее" if models.get("combine") == "mean" else "максимум"
    sheet["A9"] = (f"Индекс коррозии на НКТ = ОКРУГЛ({combined} из прогноза НТЦ и мат. модели × 100); мат. модель — "
                   f"вероятность отказа за {models.get('horizon_days', '—')} сут. Срок мероприятия отсчитывается от "
                   "даты, когда скважине впервые назначено текущее мероприятие. Пороги и сроки — переменные MAI_RATING_*.")
    sheet["A10"] = (f"Защита (режим {protection.get('mode', '—')}): эффективна, если ингибитор ≥ "
                    f"{protection.get('min_inhibitor', '—')} при отклонении закачки < {protection.get('max_injection_dev', '—')} % "
                    f"или НКТ в защитном исполнении, и это подтверждено скоростью коррозии < {protection.get('max_rate', '—')} мм/год "
                    f"без коррозии в последнем акте. Тогда баллы среды учитываются с коэффициентом {protection.get('factor', '—')}.")
    sheet["A9"].font = sheet["A10"].font = F_NOTE
    for letter, width in zip("ABCDEF", (40, 8, 11, 11, 11, 13)):
        sheet.column_dimensions[letter].width = width


def build_workbook(payload: dict) -> bytes:
    """Workbook bytes for the GET /rating payload (wells already ranked and evaluated)."""
    generated = datetime.now(timezone.utc)
    wells = payload.get("wells", [])
    book = Workbook()
    _rating_sheet(book, wells, generated)
    _schedule_sheet(book, wells, generated.date())
    _summary_sheet(book, wells, payload.get("counts", {}))
    _sources_sheet(book, wells)
    _parameters_sheet(book, payload)
    for sheet in book.worksheets:
        sheet.sheet_properties.tabColor = {"Рейтинг": NAVY, "График ППР": BLUE}.get(sheet.title, "8FA3B8")
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
    book.active = 0
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()
