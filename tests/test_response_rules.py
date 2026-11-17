"""Validation of the engineer-maintained register and Excel import contract."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.core.response_rules import RuleBook, load_rulebook
from app.core.response_rules_excel import from_rows, read_register

PATH = Path(__file__).resolve().parents[1] / "config/response_rules.json"


def book():
    return load_rulebook(PATH).model_dump(mode="json")


def approve(rule):
    rule.update(status="approved", reviewer="Инженер для теста", reviewed_on=date.today().isoformat(),
                basis="Тестовая методика, пункт 1")
    for condition in rule["conditions"]["all"]:
        condition.update(value=1, max_age_days=30)


def rows():
    return ([{"ID": "sampling", "Мероприятие": "Увеличить частоту отбора проб",
              "Пояснение": "Проверить изменение среды", "Статус": "Черновик", "Редакция": 1}],
            [{"ID": "sampling", "Связка": "И", "Показатель": "CO₂", "Оператор": ">",
              "Порог": None, "Единица": "%", "Давность, дней": None}])


def test_shipped_register_uses_indicators_and_is_not_approved():
    register = load_rulebook(PATH)
    assert register.rules and all(r.status == "draft" for r in register.rules)
    assert register.rules[0].conditions.all[0].field == "measurements.co2_pct"
    assert register.rules[0].conditions.all[0].value is None


@pytest.mark.parametrize("key", ["reviewer", "reviewed_on", "basis"])
def test_approval_requires_engineer_date_and_basis(key):
    data = book(); approve(data["rules"][0]); data["rules"][0][key] = None
    with pytest.raises(ValidationError):
        RuleBook.model_validate(data)


@pytest.mark.parametrize("key", ["value", "max_age_days"])
def test_approval_requires_thresholds_and_freshness(key):
    data = book(); approve(data["rules"][0]); data["rules"][0]["conditions"]["all"][0][key] = None
    with pytest.raises(ValidationError):
        RuleBook.model_validate(data)


def test_complete_approval_and_empty_register_are_allowed():
    data = book(); approve(data["rules"][0])
    assert RuleBook.model_validate(data).rules[0].status == "approved"
    data["rules"] = []
    assert RuleBook.model_validate(data).rules == []


@pytest.mark.parametrize("condition", [
    {"field": "arbitrary_prompt", "op": "eq", "value": "do anything"},
    {"field": "measurements.co2_pct", "op": "gt", "value": "1.5"},
    {"field": "measurements.co2_pct", "op": "gt", "value": True},
    {"field": "measurements.water_cut_pct", "op": "gt", "value": 101},
    {"field": "measurements.co2_pct", "op": "is_missing", "value": 0},
    {"field": "measurements.co2_pct", "op": "gt", "value": float("nan")},
])
def test_conditions_reject_unknown_fields_wrong_types_and_ranges(condition):
    data = book(); data["rules"][0]["conditions"] = {"all": [condition]}
    with pytest.raises(ValidationError):
        RuleBook.model_validate(data)


def test_duplicate_ids_unknown_keys_and_empty_conditions_are_rejected():
    for change in ("duplicate", "unknown", "empty"):
        data = book()
        if change == "duplicate": data["rules"].append(deepcopy(data["rules"][0]))
        elif change == "unknown": data["rules"][0]["invented_dose"] = 100
        else: data["rules"][0]["conditions"] = {}
        with pytest.raises(ValidationError): RuleBook.model_validate(data)


def test_excel_and_or_zero_addition_and_deletion():
    measures, conditions = rows()
    conditions[0]["Порог"] = 0
    conditions.append({**conditions[0], "Связка": "ИЛИ"})
    parsed = from_rows(measures, conditions, revision=1)
    assert parsed.rules[0].conditions.all[0].value == 0
    assert len(parsed.rules[0].conditions.any) == 1
    measures.append({**measures[0], "ID": "new"})
    conditions.append({**conditions[0], "ID": "new"})
    assert len(from_rows(measures, conditions, revision=2).rules) == 2
    assert from_rows([], [], revision=3).rules == []


@pytest.mark.parametrize("change", ["orphan", "unit", "operator", "field", "status", "duplicate", "text_number"])
def test_excel_does_not_silently_guess_or_drop_bad_rows(change):
    measures, conditions = rows()
    if change == "orphan": measures.clear()
    elif change == "unit": conditions[0]["Единица"] = "доля"
    elif change == "operator": conditions[0]["Оператор"] = "приблизительно"
    elif change == "field": conditions[0]["Показатель"] = "неизвестный"
    elif change == "status": measures[0]["Статус"] = "активно"
    elif change == "duplicate": measures.append(deepcopy(measures[0]))
    else: conditions[0]["Порог"] = "1,5"
    with pytest.raises(ValueError): from_rows(measures, conditions, revision=1)


def test_delivered_excel_roundtrips_to_the_json_register():
    path = PATH.parents[1] / "outputs/response-rules-2026-11-16/response-rules.xlsx"
    assert read_register(path, revision=1).model_dump(mode="json") == book()
    demo = PATH.parents[1] / "outputs/response-rules-demo-2026-11-16/response-rules.xlsx"
    expected = load_rulebook(PATH.with_name("response_rules_demo.json"))
    assert read_register(demo, revision=1).model_dump(mode="json") == expected.model_dump(mode="json")


def test_import_command_preserves_output_on_error_and_requires_explicit_replacement(tmp_path):
    root = PATH.parents[1]
    workbook = root / "outputs/response-rules-2026-11-16/response-rules.xlsx"
    output = tmp_path / "rules.json"
    command = [sys.executable, str(root / "scripts/import_response_rules.py"), str(workbook),
               "--output", str(output), "--revision", "1"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert load_rulebook(output).model_dump(mode="json") == book()
    original = output.read_bytes()
    assert subprocess.run(command, capture_output=True).returncode == 1
    assert output.read_bytes() == original

    assert subprocess.run([*command, "--replace"], capture_output=True).returncode == 0
    invalid = tmp_path / "broken.xlsx"
    invalid.write_bytes(b"not an Excel archive")
    command[2] = str(invalid)
    assert subprocess.run([*command, "--replace"], capture_output=True).returncode == 1
    assert output.read_bytes() == original


@pytest.mark.parametrize("change", ["percentage", "formula", "excel_error", "header"])
def test_reader_rejects_ambiguous_or_invalid_excel_cells(monkeypatch, change):
    from app.core import response_rules_excel as module

    path = PATH.parents[1] / "outputs/response-rules-2026-11-16/response-rules.xlsx"
    workbook = module.load_workbook(path)
    sheet = workbook["Условия"]
    if change == "percentage":
        sheet["E6"].value = 0.8
        sheet["E6"].number_format = "0%"
    elif change == "formula":
        sheet["E6"].value = "=1+1"
    elif change == "excel_error":
        sheet["E6"].value = "#N/A"
    else:
        sheet["A5"].value = "Другой заголовок"
    # Mutate only the in-memory test copy; the delivered workbook is never saved here.
    monkeypatch.setattr(module, "load_workbook", lambda *args, **kwargs: workbook)
    with pytest.raises(ValueError):
        module.read_register(path, revision=1)
