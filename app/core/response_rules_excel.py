"""Read the engineer's Excel register. No LLM interpretation of conditions."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

from app.core.response_rules import FIELDS, RuleBook

RULE_HEADERS = ("ID", "Мероприятие", "Пояснение", "Статус", "Редакция", "Инженер", "Дата проверки", "Основание")
CONDITION_HEADERS = ("ID", "Связка", "Показатель", "Оператор", "Порог", "Единица", "Давность, дней")
STATUS_LABELS = {"Черновик": "draft", "На проверке": "review", "Согласовано": "approved", "Архив": "retired", "Тестовое": "test"}
OPERATORS = {"=": "eq", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "Нет данных": "is_missing"}
LABELS = {indicator.name: key for key, indicator in FIELDS.items()}
HEADER_ROW = 5


def from_rows(measures: list[dict], conditions: list[dict], *, revision: int) -> RuleBook:
    rules = {}
    for row in measures:
        key = row.get("ID")
        if not isinstance(key, str) or key in rules:
            raise ValueError(f"Неверный или повторяющийся ID мероприятия: {key}")
        status = row.get("Статус")
        if status not in STATUS_LABELS:
            raise ValueError(f"{key}: неизвестный статус {status}")
        checked = row.get("Дата проверки")
        if isinstance(checked, datetime):
            checked = checked.date()
        rules[key] = dict(id=key, revision=row.get("Редакция"), action=row.get("Мероприятие"), explanation=row.get("Пояснение"),
                          status=STATUS_LABELS[status], reviewer=row.get("Инженер"),
                          reviewed_on=checked, basis=row.get("Основание"), conditions={"all": [], "any": []})
    for number, row in enumerate(conditions, HEADER_ROW + 1):
        key, label = row.get("ID"), row.get("Показатель")
        if key not in rules:
            raise ValueError(f"Условия, строка {number}: неизвестный ID {key}; удалите также его условия")
        if label not in LABELS or row.get("Связка") not in ("И", "ИЛИ") or row.get("Оператор") not in OPERATORS:
            raise ValueError(f"Условия, строка {number}: неизвестный показатель, связка или оператор")
        field = LABELS[label]
        if row.get("Единица") != (FIELDS[field].unit or "доля"):
            raise ValueError(f"Условия, строка {number}: неверная единица для {label}")
        group = "all" if row["Связка"] == "И" else "any"
        rules[key]["conditions"][group].append(dict(field=field, op=OPERATORS[row["Оператор"]],
                                                  value=row.get("Порог"), max_age_days=row.get("Давность, дней")))
    return RuleBook.model_validate(dict(schema_version=2, revision=revision, rules=list(rules.values())))


def read_register(path: Path, *, revision: int) -> RuleBook:
    with Path(path).open("rb") as stream:
        workbook = load_workbook(stream, data_only=False, read_only=True)
        try:
            result = []
            for name, headers in (("Мероприятия", RULE_HEADERS), ("Условия", CONDITION_HEADERS)):
                if name not in workbook.sheetnames:
                    raise ValueError(f"Нет листа {name}")
                sheet = workbook[name]
                actual = tuple(cell.value for cell in next(sheet.iter_rows(min_row=HEADER_ROW, max_row=HEADER_ROW)))
                if actual[:len(headers)] != headers or any(v is not None for v in actual[len(headers):]):
                    raise ValueError(f"{name}: не меняйте заголовки в строке {HEADER_ROW}")
                rows = []
                for cells in sheet.iter_rows(min_row=HEADER_ROW + 1):
                    if all(cell.value is None for cell in cells):
                        continue
                    if any(cell.data_type in ("f", "e") for cell in cells):
                        raise ValueError(f"{name}, строка {cells[0].row}: формулы и ошибки Excel не допускаются")
                    if name == "Условия" and cells[4].value is not None and "%" in cells[4].number_format:
                        raise ValueError(f"{name}, строка {cells[0].row}: введите порог обычным числом, например 80 вместо 80%")
                    if any(cell.value is not None for cell in cells[len(headers):]):
                        raise ValueError(f"{name}: данные за пределами столбцов шаблона")
                    rows.append(dict(zip(headers, (cell.value for cell in cells))))
                result.append(rows)
            return from_rows(*result, revision=revision)
        finally:
            workbook.close()
