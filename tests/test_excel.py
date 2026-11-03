"""Excel without AI: source exports recognised among documents, other tables and forms, Excel 97-2003
and the НКТ registry — read directly and through the documents API."""
from __future__ import annotations

from datetime import timedelta
from io import BytesIO

import pytest

from tests.conftest import TODAY, _gap_keys, _upload, _well, needs_db

from app.core.documents import extract


@needs_db
def test_lab_protocol_excel_form_counts_as_samples(client, generated):
    assert "sampling" in _gap_keys(_well(client, "DEMO-012"))
    document = _upload(client, generated / "documents" / "protokol_khim_analiza_15.xlsx")
    assert document["status"] == "loaded", document["status_message"]
    # The table under the protocol title is found by its header row: a ХИМИК import, no AI.
    assert document["doc_type"] == "source:khimik"
    assert document["well_ids"] == ["DEMO-012", "DEMO-013"]
    well = _well(client, "DEMO-012")
    assert well["sampling"] is True
    assert "sampling" not in _gap_keys(well)


@needs_db
def test_scanned_pdf_and_source_table(client, generated, tmp_path):
    from PIL import Image

    pdf = tmp_path / "akt_demontazha_scan.pdf"
    Image.open(generated / "documents" / "akt_demontazha_DEMO-005_scan.jpg").convert("RGB").save(pdf, "PDF", resolution=150)
    document = _upload(client, pdf)
    assert document["status"] == "loaded", document["status_message"]
    assert document["text_method"] == "ocr"

    # A ХИМИК export dropped among documents goes through the usual source import.
    table = _upload(client, generated / "sample_khimik.xlsx")
    assert table["status"] == "loaded", table["status_message"]
    assert table["doc_type"] == "source:khimik"
    assert table["type_title"] == "Выгрузка KHIMIK"
    assert len(table["well_ids"]) >= 10


@needs_db
def test_source_import_recognises_a_column_by_its_name(client, tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Скважина", "Дата отбора", "CO2, %", "Тоннаж реагента, т", "Эффективность ингибитора, %", "pH"])
    book.active.append(["DEMO-012", TODAY.strftime("%d.%m.%Y"), 1.4, 6.5, 85, 6.8])
    book.save(tmp_path / "khimik.xlsx")
    with open(tmp_path / "khimik.xlsx", "rb") as handle:
        result = client.post("/api/v1/ingestion/excel", data={"profile_id": "khimik"},
                             files={"file": ("khimik.xlsx", handle)}).json()
    assert result["source_columns"]["tonnage_t"] == "Тоннаж реагента, т"
    assert result["extra_columns"] == ["pH"]                # only what has no field stays additional
    well = _well(client, "DEMO-012")
    assert well["tonnage_t"] == 6.5


# --------------------------------------------------------------------------- mixed chemistry tables
def _chemistry_book(path, date_header="Дата", title=None, text_dates=True):
    from datetime import datetime as _dt
    from openpyxl import Workbook

    when = TODAY - timedelta(days=3)
    value = when.strftime("%d.%m.%Y") if text_dates else _dt.combine(when, _dt.min.time()).replace(hour=10)
    book = Workbook()
    sheet = book.active
    for line in title or []:
        sheet.append([line])
    if title:
        sheet.append([])
    sheet.append(["Скважина", date_header, "CO2, %", "pH", "Обводнённость, %", "Хлориды, мг/л", "Жёсткость, мг-экв/л", "Куст"])
    sheet.append(["DEMO-012", value, 1.4, 6.8, 47, 21000, 125, 7])
    sheet.append(["DEMO-013", value, 1.2, 7.1, 52, 19500, 98, 7])
    book.save(path)
    return path


def test_numbers_are_not_merged_across_table_cells():
    assert extract.value_agrees(21000.0, "number", "DEMO-012 | 16.10.2026 | 1.4 | 6.8 | 47 | 21000 | 125", "x")
    assert not extract.value_agrees(21000125.0, "number", "DEMO-012 | 47 | 21000 | 125", "x")
    assert extract.value_agrees(18500.0, "number", "Хлориды: 18 500 мг/л", "x")


def test_text_dates_in_excel():
    from app.core.excel_cells import as_datetime as _as_datetime

    assert _as_datetime("16.10.2026").date().isoformat() in {"2026-10-15", "2026-10-16"}   # local midnight in UTC
    assert _as_datetime("16.10.2026 10:30").hour == 7                                        # Moscow → UTC
    with pytest.raises(ValueError):
        _as_datetime("вчера")


def test_chemistry_only_table_is_a_khimik_sample(tmp_path):
    from app.core.documents.text import detect_profile
    from app.core.excel_ingestion import parse_excel

    path = _chemistry_book(tmp_path / "chem.xlsx")
    assert detect_profile(path.read_bytes(), path.name) == "khimik"
    parsed = parse_excel(path.read_bytes(), path.name, "khimik")
    assert parsed["rows_accepted"] == 2
    assert parsed["extra_columns"] == ["pH", "Жёсткость, мг-экв/л"]          # «Куст» is an identifier
    point = parsed["groups"]["DEMO-012"]["records"][0]
    assert point.extra_measurements == {"pH": 6.8, "Жёсткость, мг-экв/л": 125.0}


@needs_db
def test_mixed_chemistry_table_through_documents(client, tmp_path):
    document = _upload(client, _chemistry_book(tmp_path / "analizy.xlsx"))
    assert document["status"] == "loaded", document["status_message"]
    assert document["doc_type"] == "source:khimik"
    detail = client.get(f"/api/v1/documents/{document['document_id']}").json()
    assert detail["loaded"]["extra_columns"] == ["pH", "Жёсткость, мг-экв/л"]
    assert "Куст" in detail["loaded"]["unused_columns"]
    well = _well(client, "DEMO-012")
    assert well["sampling"] is True and "sampling" not in _gap_keys(well)
    extras = {item["name"]: item for item in well["extra_measurements"]}
    assert extras["pH"]["value"] == 6.8 and extras["pH"]["source"] == "ХИМИК"
    assert extras["pH"]["date"][:10] == (TODAY - timedelta(days=3)).isoformat()


@needs_db
def test_protocol_with_other_indicators(client, tmp_path, fake):
    path = _chemistry_book(tmp_path / "protokol.xlsx", title=["ПРОТОКОЛ ХИМИЧЕСКОГО АНАЛИЗА № 21"])
    document = _upload(client, path)
    assert document["status"] == "loaded", document["status_message"]
    assert document["doc_type"] == "source:khimik"
    assert not [call for call in fake.calls if call["kind"] in {"extract", "classify"}]
    detail = client.get(f"/api/v1/documents/{document['document_id']}").json()
    assert detail["loaded"]["extra_columns"] == ["pH", "Жёсткость, мг-экв/л"]
    extras = {item["name"]: item for item in _well(client, "DEMO-012")["extra_measurements"]}
    assert extras["pH"]["value"] == 6.8 and extras["pH"]["source"] == "ХИМИК"
    # The AI agent sees them as context.
    from app.core.proposal_agent import build_context
    assert any(item["name"] == "pH" for item in build_context("DEMO-012")["other_measurements"])


# --------------------------------------------------------------------------- Excel without AI
def _book(path, rows):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Анализы"
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


def _custom_table(path, date_in_title=True, wells=("DEMO-012", "DEMO-013")):
    when = (TODAY - timedelta(days=2)).strftime("%d.%m.%Y")
    rows = [["Результаты анализа попутной воды"]]
    if date_in_title:
        rows.append([f"Дата отбора проб: {when}"])
    rows += [[], ["№ п/п", "№ скв.", "Обводн., %", "Cl-, мг/л", "Содержание CO2, %", "pH", "Fe общ., мг/л", "Эффективность ингибитора, %"]]
    for index, well in enumerate(wells, start=1):
        rows.append([index, well, 40 + index, f"{20 + index} 500", 1.1 + index / 10, 6.5 + index / 10, 0.3, 86])
    return _book(path, rows)


def test_excel_table_with_custom_headers_and_date_in_title(tmp_path):
    from app.core.documents.excel_reader import read_workbook

    result = read_workbook(_custom_table(tmp_path / "t.xlsx").read_bytes(), ["DEMO-012", "DEMO-013"])
    assert result["layout"] == "table" and result["accepted"] == 2
    record = result["records"][0]
    fields = {name: entry["value"] for name, entry in record["fields"].items() if entry["status"] == "ok"}
    assert record["well_id"] == "DEMO-012"
    assert fields["sample_date"] == (TODAY - timedelta(days=2)).isoformat()
    assert fields["water_cut_pct"] == 41 and fields["chlorides_mg_l"] == 21500 and fields["co2_pct"] == 1.2
    assert fields["inhibitor_efficiency"] == 0.86                     # percent -> fraction
    assert {item["name"] for item in fields["extra_measurements"]} == {"pH", "Fe общ., мг/л"}   # «№ п/п» skipped
    assert record["fields"]["chlorides_mg_l"]["quote"].startswith("Лист «Анализы», D5")


def test_excel_numeric_well_new_well_and_missing_date(tmp_path):
    from app.core.documents.excel_reader import not_found_reason, read_workbook

    path = _book(tmp_path / "n.xlsx", [["Скважина", "Дата", "Хлориды, мг/л"], [1234, TODAY, 21000], [5555, TODAY, 20000],
                                       ["скв. Б/Н", TODAY, 19000]])
    result = read_workbook(path.read_bytes(), ["1234"])
    known, new, unnamed = result["records"]
    assert known["well_id"] == "1234" and known["accepted"] and not known["new_well"]
    # A well the system does not know yet: the document is its first source (a number is needed).
    assert new["well_id"] == "5555" and new["accepted"] and new["new_well"]
    assert not unnamed["accepted"] and "Не распознан номер скважины «скв. Б/Н»." in unnamed["issues"]
    no_date = read_workbook(_custom_table(tmp_path / "d.xlsx", date_in_title=False).read_bytes(), ["DEMO-012", "DEMO-013"])
    assert no_date["accepted"] == 0 and "Не найдена дата" in not_found_reason(no_date)
    nothing = read_workbook(_book(tmp_path / "x.xlsx", [["Итоги квартала"], ["План", 10]]).read_bytes(), ["DEMO-012"])
    assert "не найдена скважина" in not_found_reason(nothing)


def _xls(path, rows, merge=None):
    """An Excel 97-2003 workbook (xlwt): dates with a date format, as Excel writes them."""
    import xlwt
    from datetime import date as _date, datetime as _datetime

    book = xlwt.Workbook()
    sheet = book.add_sheet("Анализы")
    date_style = xlwt.easyxf(num_format_str="DD.MM.YYYY")
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            if isinstance(value, (_date, _datetime)):
                sheet.write(row_index, column, value, date_style)
            elif value is not None:
                sheet.write(row_index, column, value)
    if merge:
        sheet.merge(*merge)
    book.save(str(path))
    return path


def test_xls_converts_to_xlsx_with_types_and_merges(tmp_path):
    from openpyxl import load_workbook

    from app.core.excel_legacy import is_legacy_workbook, xls_to_xlsx

    path = _xls(tmp_path / "a.xls", [["Реестр", None, None], ["Скважина", "Дата", "Хлориды"], [7781, TODAY, 21000.5]],
                merge=(0, 0, 0, 2))
    content = path.read_bytes()
    assert is_legacy_workbook(content)
    sheet = load_workbook(BytesIO(xls_to_xlsx(content))).active
    assert sheet.title == "Анализы" and sheet["A1"].value == "Реестр"
    assert sheet["A3"].value == 7781 and isinstance(sheet["A3"].value, int)       # not 7781.0
    assert sheet["B3"].value.date() == TODAY and sheet["C3"].value == 21000.5
    assert [str(item) for item in sheet.merged_cells.ranges] == ["A1:C1"]
    with pytest.raises(ValueError):
        xls_to_xlsx(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600)


@needs_db
def test_xls_through_documents_and_source_import(client, generated, tmp_path):
    from openpyxl import load_workbook

    # A laboratory table saved as Excel 97-2003, through «Документы».
    when = TODAY - timedelta(days=2)
    rows = [["Скважина", "Дата отбора", "Обводнённость, %", "Хлориды, мг/л", "CO2, %"],
            ["DEMO-012", when, 47, 21000, 1.4], ["DEMO-013", when, 52, 19500, 1.2]]
    document = _upload(client, _xls(tmp_path / "analizy.xls", rows))
    assert document["status"] == "loaded", document["status_message"]
    assert document["file_kind"] == "xls" and document["doc_type"] == "source:khimik"   # standard columns: ХИМИК
    assert _well(client, "DEMO-012")["sampling"] is True

    # A ДАРС export saved as .xls, through «Загрузка данных» (source profile).
    source = load_workbook(generated / "sample_dars.xlsx", data_only=True)
    sheet = source.active
    xls_path = _xls(tmp_path / "dars.xls", [list(row) for row in sheet.iter_rows(values_only=True)])
    with open(generated / "sample_dars.xlsx", "rb") as handle:
        as_xlsx = client.post("/api/v1/ingestion/excel", data={"profile_id": "dars"},
                              files={"file": ("dars.xlsx", handle)}).json()
    with open(xls_path, "rb") as handle:
        response = client.post("/api/v1/ingestion/excel", data={"profile_id": "dars"}, files={"file": ("dars.xls", handle)})
    assert response.status_code == 201, response.text
    as_xls = response.json()
    assert as_xls["rows_accepted"] == as_xlsx["rows_accepted"] > 0
    # A Word 97-2003 file has the same container as .xls: it is still refused.
    rejected = client.post("/api/v1/documents?wait=true", files=[("files", ("notes.doc", b"\xd0\xcf\x11\xe0" + b"\0" * 600))])
    assert rejected.json()["documents"][0]["error"] == "Файлы .doc не поддерживаются: сохраните документ как PDF."


def _form(path, well="DEMO-012"):
    when = (TODAY - timedelta(days=1)).strftime("%d.%m.%Y")
    return _book(path, [["ПРОТОКОЛ ИСПЫТАНИЙ № 7"], [f"Скважина № {well}"], ["Дата отбора", when], [],
                        ["Показатель", "Ед. изм.", "Значение"], ["Обводнённость", "%", 48], ["Хлориды", "мг/л", "21 000"],
                        ["pH", None, "6,9"], ["Сульфаты", "мг/л", 140], ["Номер пробы", None, 15]])


def test_excel_form_with_one_sample(tmp_path):
    from app.core.documents.excel_reader import read_workbook

    result = read_workbook(_form(tmp_path / "f.xlsx").read_bytes(), ["DEMO-012"])
    assert result["layout"] == "form" and result["accepted"] == 1
    fields = {name: entry["value"] for name, entry in result["records"][0]["fields"].items() if entry["status"] == "ok"}
    assert fields["water_cut_pct"] == 48 and fields["chlorides_mg_l"] == 21000
    assert {item["name"] for item in fields["extra_measurements"]} == {"pH", "Сульфаты, мг/л"}


@needs_db
def test_excel_documents_load_without_llm(client, tmp_path, monkeypatch):
    from app.config import settings
    from app.core import llm

    def no_llm(*args, **kwargs):
        raise AssertionError("Excel must not call the LLM")

    monkeypatch.setattr(llm, "chat", no_llm)
    monkeypatch.setattr(type(settings), "extraction_model", property(lambda self: ""))
    table = _upload(client, _custom_table(tmp_path / "analizy.xlsx"))
    assert table["status"] == "loaded", table["status_message"]
    assert table["doc_type"] == "excel:table" and table["type_title"] == "Таблица показателей (Excel)"
    well = _well(client, "DEMO-012")
    assert well["sampling"] is True and "sampling" not in _gap_keys(well)
    assert {"pH", "Fe общ., мг/л"} <= {item["name"] for item in well["extra_measurements"]}
    form = _upload(client, _form(tmp_path / "forma.xlsx", well="DEMO-013"))
    assert form["status"] == "loaded" and form["doc_type"] == "excel:form", form["status_message"]
    # Wells the system does not know yet: the document creates them, and its deletion removes them.
    new = _upload(client, _custom_table(tmp_path / "novye.xlsx", wells=("X-1", "X-2")))
    assert new["status"] == "loaded" and "Новых скважин в системе: 2 (X-1, X-2)." in new["status_message"]
    assert _well(client, "X-1")["sampling"] is True
    assert client.delete(f"/api/v1/documents/{new['document_id']}").status_code == 200
    assert client.get("/api/v1/rating/wells/X-1").status_code == 404
    assert "X-1" not in {item["well_id"] for item in client.get("/api/v1/rating").json()["wells"]}
    detail = client.get(f"/api/v1/documents/{table['document_id']}").json()
    assert detail["field_labels"]["chlorides_mg_l"] == "Хлориды, мг/л"


# --------------------------------------------------------------------------- Реестр коррозии НКТ
REGISTRY_HEAD = ["№", "ДО", "Месторождение", "Куст", "Скважина", "Глубина спуска насоса, м", "Пласт",
                 "Факты выявления коррозии НКТ на скважине", "ННО, сут", "Режим работы ЭЦН", "Признак новизны НКТ",
                 "Диаметр НКТ", "Группа прочности поврежденной НКТ",
                 "Состав подвески(состоит из 3 Материал НКТ группа прочности вверх, середина, низ)", None, None,
                 "Температура на устье, оС", "Температура пластовая, оС", "Давление на устье, атм",
                 "Средний дебит жидкости, м3", "Средняя обводненность, %", "Плотность воды, кг/м3", "Ca2+", "Mg2+",
                 "Na+ K+", "Feобщ", "Cl-", "SO42-", "HCO3-", "CO32", "Общая минерализация, мг/л", "pH воды, ед",
                 "Среднее значение КВЧ, мг/л", "CO2, мл/г", "H2S, мг/л", "CO2, % мол.", "H2S % мол.",
                 "Газовый фактор м3/т", "марка стали", "Завод", "Отказы за весь период, кол-во", "Тип повреждения",
                 "Глубина от устья, где произошел отказ, м", "Толщина стенки поврежденной НКТ, мм"]


def _registry(path, rows, title=None):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Реестр"
    if title:
        sheet.append([title])
    sheet.append(REGISTRY_HEAD)
    sub = [None] * len(REGISTRY_HEAD)
    sub[13:16] = ["верх", "середина", "низ"]
    sheet.append(sub)
    sheet.append(list(range(1, len(REGISTRY_HEAD) + 1)))      # column numbers under the header
    header_row = 2 if title else 1
    sheet.merge_cells(start_row=header_row, start_column=14, end_row=header_row, end_column=16)
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


def _registry_row(no, well, field="Южное", water_cut=92, chlorides=9800):
    return [no, "ООО «Нефть-Юг»", field, "12", well, 2150, "ЮВ1", "да", 340, "постоянный", "новые", 73, "К",
            "К", "Е", "Н80", 38, 92, 14.5, 85, water_cut, 1012, 420, 95, 6100, 12.4, chlorides, 45, 610, 0, 17500,
            6.8, 120, 0.8, 2.1, 1.9, 0.01, 85, "Ст20", "ТМК", 3, "свищ", 1840, 5.5]


def test_registry_columns_and_two_row_header(tmp_path):
    from app.core.documents import nkt_registry

    when = (TODAY - timedelta(days=3)).strftime("%d.%m.%Y")
    path = _registry(tmp_path / "r.xlsx", [_registry_row(1, "DEMO-001"), _registry_row(2, "7781"),
                                           _registry_row(3, "7781", field="Северное"),
                                           [4, "ООО «Нефть-Юг»", "Южное", "14", "9001"]],
                     title=f"Реестр фактов коррозии НКТ по состоянию на {when}")
    content = path.read_bytes()
    assert nkt_registry.detect(content)
    assert not nkt_registry.detect(_custom_table(tmp_path / "t.xlsx").read_bytes())
    result = nkt_registry.read_workbook(content, ["DEMO-001"], TODAY)
    assert result["date"] == TODAY - timedelta(days=3) and result["date_note"] is None
    [record] = result["records"]
    values = {key: field["value"] for key, field in record["fields"].items()}
    assert record["well_id"] == "DEMO-001" and not record["new_well"]
    assert values["string_top"] == "К" and values["string_middle"] == "Е" and values["string_bottom"] == "Н80"
    assert values["cl"] == 9800 and values["co2_mol_pct"] == 1.9 and values["co2_ml_g"] == 0.8
    assert values["h2s_mg_l"] == 2.1 and values["h2s_mol_pct"] == 0.01 and values["co3"] == 0
    assert values["nno_days"] == 340 and values["failures_count"] == 3 and values["failure_depth_m"] == 1840
    assert values["corrosion_facts"] == "да" and values["damage_type"] == "свищ" and values["steel_grade"] == "Ст20"
    assert "row_no" not in values
    assert record["fields"]["cl"]["quote"].startswith("Лист «Реестр», AA")
    assert result["columns"]["system"] == ["Месторождение", "Средний дебит жидкости, м3", "Средняя обводненность, %",
                                           "Cl-", "CO2, % мол."]
    issues = {issue for item in result["rejected"] for issue in item["issues"]}
    assert any("разных месторождениях" in issue for issue in issues)            # 7781 on two fields
    assert "В строке нет данных, кроме расположения скважины." in issues          # 9001: location only
    no_date = nkt_registry.read_workbook(_registry(tmp_path / "n.xlsx", [_registry_row(1, "5")]).read_bytes(), [], TODAY)
    assert no_date["date"] == TODAY and "дата" in no_date["date_note"].casefold()
    assert no_date["records"][0]["new_well"] is False                          # no well list: taken as is


def test_registry_well_header_with_latin_letters(tmp_path):
    from app.core.documents import nkt_registry

    # «Cкважина» typed with a Latin C (and «Скв. №» with a Latin «C» and «к»): still the well column.
    for title in ("Cкважина", "Cкв. №", "Скважина (№)"):
        head = ["№", "ДО", "Месторождение", "Куст", title, *REGISTRY_HEAD[5:]]
        path = _registry(tmp_path / "r.xlsx", [_registry_row(1, "DEMO-001")])
        from openpyxl import load_workbook

        book = load_workbook(path)
        for column, value in enumerate(head, start=1):
            book.active.cell(row=1, column=column, value=value)
        book.save(path)
        result = nkt_registry.read_workbook(path.read_bytes(), ["DEMO-001"], TODAY)
        assert [record["well_id"] for record in result["records"]] == ["DEMO-001"], title


@needs_db
def test_registry_through_documents(client, tmp_path, monkeypatch):
    from app import db
    from app.config import settings
    from app.core import llm

    def no_llm(*args, **kwargs):
        raise AssertionError("The registry must not call the LLM")

    monkeypatch.setattr(llm, "chat", no_llm)
    monkeypatch.setattr(type(settings), "extraction_model", property(lambda self: ""))
    path = _registry(tmp_path / "reestr_nkt.xlsx", [_registry_row(1, "DEMO-012", water_cut=93, chlorides=31000),
                                                    _registry_row(2, "NEW-77")])
    document = _upload(client, path)
    assert document["status"] == "loaded", document["status_message"]
    assert document["doc_type"] == "excel:nkt_registry" and document["type_title"] == "Реестр коррозии НКТ (Excel)"
    assert document["well_ids"] == ["DEMO-012", "NEW-77"] and document["doc_date"] == TODAY.isoformat()
    assert "Новых скважин в системе: 1" in document["status_message"]
    assert "extraction" not in client.get("/api/v1/documents").json()["documents"][0]   # the list stays light
    detail = client.get(f"/api/v1/documents/{document['document_id']}").json()
    assert detail["extraction"]["records_accepted"] == 2 and detail["field_labels"]["string_top"] == "Состав подвески: верх"

    well = _well(client, "DEMO-012")
    registry = dict(well["nkt_registry"]["rows"][0])
    assert registry["Тип повреждения"] == "свищ" and registry["Состав подвески: низ"] == "Н80"
    point = [item for item in db.load_telemetry("DEMO-012") if getattr(item, "source_id", None) == "DOC_NKT"]
    assert len(point) == 1 and point[0].chlorides_mg_l == 31000 and point[0].water_cut_pct == 93
    assert point[0].co2_pct == 1.9 and point[0].liquid_rate_m3_day == 85
    assert "Реестр коррозии НКТ (документ)" in well["provenance"]["corrosive_environment"]["source"]
    new_well = _well(client, "NEW-77")
    assert new_well["field"] == "Южное"                                         # a new well gets a rating row
    # A registry row is not a measurement: no «in operation», and no ДАРС telemetry.
    assert new_well["in_operation"] is None and new_well["index"] is None
    # «Полнота данных» by field: the registry filled the environment and the liquid rate, so those
    # columns are complete without ДАРС or ХИМИК; the forecast and the status are not.
    gaps = {gap["key"]: gap for gap in new_well["data_gaps"]}
    assert "environment" not in gaps and "liquid" not in gaps
    assert gaps["forecast"]["note"].startswith("модели не рассчитаны: ")
    assert "статус работы" in gaps["well"]["note"]
    assert gaps["category"]["note"] == "нет: скорость коррозии"
    assert well["provenance"]["in_operation"]["source_id"] != "DOC_NKT"
    # The light card endpoint ranks the well exactly as the full rating does.
    ranks = {item["well_id"]: item["rank"] for item in client.get("/api/v1/rating").json()["wells"]}
    for well_id in ("DEMO-001", "DEMO-012", "NEW-77"):
        assert _well(client, well_id)["rank"] == ranks[well_id]

    assert client.delete(f"/api/v1/documents/{document['document_id']}").status_code == 200
    assert _well(client, "DEMO-012")["nkt_registry"] is None
    assert not [item for item in db.load_telemetry("DEMO-012") if getattr(item, "source_id", None) == "DOC_NKT"]
