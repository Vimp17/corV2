"""Document pipeline: quotes and checks without a database, OCR and long texts in parts, then PDFs,
scans and the inbox through the API on SQL Server (LiteLLM replaced by tests.fake_litellm)."""
from __future__ import annotations

import os
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

import pytest

from tests.conftest import TEST_TMP, TODAY, _gap_keys, _upload, _well, needs_db
from tests.fake_litellm import FakeLiteLLM

from app.core.documents import extract, intake


BASE_SPEC = {
    "title": "Акт разбора на базе", "target": "work_event", "work_type": "Акт разбора на базе",
    "source_id": "DOC_BASE",
    "fields": {
        "well_id": {"label": "Номер скважины", "type": "string", "required": True},
        "act_date": {"label": "Дата разбора", "type": "date", "required": True},
        "corrosion_detected": {"label": "Обнаружена коррозия", "type": "boolean"},
        "corrosion_type": {"label": "Вид коррозии", "type": "string"},
    },
    "informative_fields": ["corrosion_detected", "corrosion_type"],
}


TEXT = ("АКТ РАЗБОРА ОБОРУДОВАНИЯ НА БАЗЕ\nДата разбора: 12.03.2026\n| Скважина № | DEMO-002 |\n"
        "Коррозия обнаружена: да\nВид коррозии: язвенная")


def _raw(**overrides):
    record = {
        "well_id": {"value": "DEMO-002", "quote": "Скважина № | DEMO-002"},
        "act_date": {"value": "2026-03-12", "quote": "Дата разбора: 12.03.2026"},
        "corrosion_detected": {"value": True, "quote": "Коррозия обнаружена: да"},
        "corrosion_type": {"value": "язвенная", "quote": "Вид коррозии: язвенная"},
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------- no database
def test_quote_checks_tolerate_tables_and_spacing():
    text = extract.normalize(TEXT)
    assert extract.quote_found("Скважина №  DEMO-002", text)
    assert extract.quote_found("дата  разбора: 12.03.2026", text)
    assert not extract.quote_found("Скважина № DEMO-777 после ремонта", text)


def test_check_record_accepts_confirmed_values():
    record = extract.check_record(_raw(), BASE_SPEC, extract.normalize(TEXT), ["DEMO-001", "DEMO-002"],
                                  today=date(2026, 10, 19))
    assert record["accepted"], record["issues"]
    assert record["well_id"] == "DEMO-002"
    assert extract.values_of(record)["act_date"] == "2026-03-12"


def test_value_without_quote_in_text_is_dropped():
    raw = _raw(corrosion_type={"value": "общая", "quote": "Вид коррозии: общая"},
               corrosion_detected={"value": True, "quote": None})
    record = extract.check_record(raw, BASE_SPEC, extract.normalize(TEXT), ["DEMO-002"], today=date(2026, 10, 19))
    assert record["fields"]["corrosion_type"]["status"] == "quote_not_found"
    assert record["fields"]["corrosion_detected"]["status"] == "no_quote"
    assert not record["accepted"]          # no informative field left


def test_unknown_well_and_implausible_date_reject_the_record():
    raw = _raw(well_id={"value": "DEMO-999", "quote": "DEMO-002"})
    record = extract.check_record(raw, BASE_SPEC, extract.normalize(TEXT), ["DEMO-002"], today=date(2026, 10, 19))
    assert not record["accepted"]           # the quote does not support DEMO-999
    record = extract.check_record(_raw(), BASE_SPEC, extract.normalize(TEXT), ["DEMO-002"], today=date(2025, 1, 1))
    assert not record["accepted"]
    assert any("Неправдоподобная дата" in issue for issue in record["issues"])


def test_well_matching():
    known = ["DEMO-002", "1234Б", "77"]
    assert extract.match_well("скв. № demo-002", known) == "DEMO-002"
    assert extract.match_well("1234б", known) == "1234Б"
    assert extract.match_well("077", known) == "77"
    assert extract.match_well("5555", known) is None
    assert extract.match_well("5555", []) == "5555"


def test_percent_written_as_fraction_agrees_with_quote():
    assert extract.value_agrees(0.85, "number", "Эффективность ингибитора: 85 %", "inhibitor_efficiency")
    assert extract.value_agrees(18500.0, "number", "Хлориды | 18 500 мг/л", "chlorides_mg_l")
    assert not extract.value_agrees(0.5, "number", "Эффективность ингибитора: 85 %", "inhibitor_efficiency")


def test_keywords_classify_demo_texts():
    from app.core.documents.types import load_document_types
    from scripts.build_demo_documents import base_act_lines, memo_lines, passport_lines, scan_lines

    types = load_document_types()
    assert extract.classify_by_keywords("\n".join(base_act_lines(TODAY)), types) == "base_inspection_act"
    assert extract.classify_by_keywords("\n".join(scan_lines(TODAY)), types) == "demount_act"
    assert extract.classify_by_keywords("\n".join(passport_lines(TODAY)), types) == "well_passport"
    assert extract.classify_by_keywords("\n".join(memo_lines(TODAY)), types) is None


def test_ocr_request_is_one_png_data_url_in_json(monkeypatch):
    import base64

    from PIL import Image

    from app.core import llm
    from app.core.documents import text

    sent = {}

    def chat(messages, **options):
        sent.update(messages=messages, **options)
        return "MAI 4721"

    monkeypatch.setattr(llm, "chat", chat)
    assert text.ocr_image(Image.new("RGBA", (3000, 1000), "white"), "Страница 1") == "MAI 4721"
    images = [part for message in sent["messages"] if isinstance(message["content"], list)
              for part in message["content"] if part["type"] == "image_url"]
    assert len(images) == 1                       # one image per request
    url = images[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    png = base64.b64decode(url.split(",", 1)[1], validate=True)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert max(Image.open(BytesIO(png)).size) == 2000   # MAI_OCR_MAX_SIDE_PX


def test_file_kinds():
    assert intake.detect_kind(b"%PDF-1.7 ...", "a.pdf") == "pdf"
    assert intake.detect_kind(b"\x89PNG\r\n\x1a\n....", "scan.png") == "image"
    assert intake.detect_kind(b"\xd0\xcf\x11\xe0....", "old.xls") == "xls"        # Excel 97-2003
    with pytest.raises(intake.DocumentRejected):
        intake.detect_kind(b"\xd0\xcf\x11\xe0....", "old.doc")
    with pytest.raises(intake.DocumentRejected):
        intake.detect_kind(b"PK\x03\x04....", "letter.docx")


def test_long_text_is_split_between_pages_then_lines():
    pages = [f"=== Страница {number} (OCR) ===\n" + "\n".join(f"Пункт {number}.{index}: отклонений нет"
                                                              for index in range(40)) for number in (1, 2, 3)]
    text = "\n\n".join(pages)
    parts = extract.split_text(text, 2 * len(pages[0]) + 10)
    assert [part.split("\n", 1)[0] for part in parts] == ["=== Страница 1 (OCR) ===", "=== Страница 3 (OCR) ==="]
    parts = extract.split_text(text, 300)                 # pages longer than a part: cut between lines
    assert "".join(parts) == text and all(len(part) <= 300 and part.endswith("\n") for part in parts[:-1])
    assert extract.split_text("а" * 2500, 1000) == ["а" * 1000, "а" * 1000, "а" * 500]


def _long_act() -> str:
    """A demount act whose well and date are on the first page and the corrosion finding on the last."""
    from scripts.build_demo_documents import scan_lines

    lines = scan_lines(TODAY)
    filler = [f"Пункт {index}: осмотр трубы НКТ № {index}, отклонений нет." for index in range(150)]
    return "\n".join(lines[:6] + filler + lines[6:])


def test_long_document_goes_to_the_model_in_parts(monkeypatch):
    import dataclasses
    import json

    from app.core import llm
    from app.core.documents.types import load_document_types

    spec, text, stub, sent = load_document_types()["demount_act"], _long_act(), FakeLiteLLM(), []

    def chat(messages, **options):
        payload = json.loads(messages[-1]["content"])
        if len(payload["document_text"]) + len(payload.get("document_start", "")) > limit:
            raise llm.ContextTooLong("Запрос не помещается в контекстное окно модели.")
        sent.append(payload)
        return stub.chat(messages, **options)

    monkeypatch.setattr(llm, "chat", chat)
    # MAI_DOCS_CHUNK_CHARS = 2000: parts of 2000 characters with the document start; then a model whose
    # window takes only 1200 characters of text — the parts it refuses are halved.
    for chunk, limit in ((2000, 2000), (6000, 1200)):
        monkeypatch.setattr(extract, "settings", dataclasses.replace(extract.settings, docs_chunk_chars=chunk))
        sent.clear()
        records = extract.extract_records(text, spec)
        assert len(sent) > 3 and "document_start" not in sent[0]
        assert all(item["document_start"].startswith("АКТ ДЕМОНТАЖА") for item in sent[1:])
        assert len(records) == 1
        record = extract.check_record(records[0], spec, extract.normalize(text), ["DEMO-005"], TODAY)
        assert record["accepted"] and record["well_id"] == "DEMO-005"          # from the first part
        assert record["fields"]["corrosion_detected"]["value"] is True        # from the last one
    limit = 100
    with pytest.raises(extract.ExtractionUnavailable, match="контекстное окно"):
        extract.extract_records(text, spec)


def test_rows_of_a_table_read_in_two_parts_are_merged():
    from app.core.documents.types import load_document_types

    def row(**values):
        return {name: {"value": value, "quote": "…"} for name, value in values.items()}

    merged = extract._merge_parts([
        row(well_id="DEMO-001", sample_date="2026-10-01", co2_pct=1.2),
        row(well_id="DEMO-001", sample_date="2026-10-01", chlorides_mg_l=21000),   # the same row, next part
        row(well_id="DEMO-001", sample_date="2026-10-01", co2_pct=1.5),            # another sample that day
        row(well_id="DEMO-002", sample_date="2026-10-01", co2_pct=1.2),
    ], load_document_types()["lab_protocol"])
    assert [(item["well_id"]["value"], item["co2_pct"]["value"], extract._value(item.get("chlorides_mg_l")))
            for item in merged] == [("DEMO-001", 1.2, 21000), ("DEMO-001", 1.5, None), ("DEMO-002", 1.2, None)]


def test_context_window_overflow_is_recognised(monkeypatch):
    import requests

    from app.core import llm

    class Response:
        status_code, body = 400, b'{"error": {"message": "This model\'s maximum context length is 4096 tokens"}}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size):
            yield self.body

        def raise_for_status(self):
            raise requests.HTTPError("400 Bad Request")

    monkeypatch.setattr(llm.requests, "post", lambda *args, **kwargs: Response())
    request = dict(messages=[{"role": "user", "content": "текст"}], model="m", url="http://llm.local/v1/chat/completions")
    with pytest.raises(llm.ContextTooLong):
        llm.chat(**request)
    Response.body = b'{"error": {"message": "Unknown model m"}}'
    with pytest.raises(llm.LLMError) as error:
        llm.chat(**request)
    assert not isinstance(error.value, llm.ContextTooLong)


@needs_db
def test_base_act_pdf_closes_the_missing_act_gap(client, generated):
    assert "acts" in _gap_keys(_well(client, "DEMO-002"))
    document = _upload(client, generated / "documents" / "akt_razbora_DEMO-002.pdf")
    assert document["status"] == "loaded", document["status_message"]
    assert document["doc_type"] == "base_inspection_act"
    assert document["text_method"] == "text_layer"
    assert document["well_ids"] == ["DEMO-002"]
    well = _well(client, "DEMO-002")
    assert "acts" not in _gap_keys(well)
    assert well["base_act"] is True and well["base_corrosion"] is True
    assert well["category"] == "corrosive"
    assert well["provenance"]["base_act"]["source"] == "Акт разбора на базе (документ)"
    assert [item["document_id"] for item in well["documents"]] == [document["document_id"]]

    # The same file again is recognised by content; reprocessing loads nothing twice.
    again = _upload(client, generated / "documents" / "akt_razbora_DEMO-002.pdf")
    assert again["duplicate"] is True and again["document_id"] == document["document_id"]
    response = client.post(f"/api/v1/documents/{document['document_id']}/reprocess?wait=true")
    assert response.json()["status"] == "loaded"
    from app import db
    acts = [event for event in db.load_history_events("DEMO-002") if event["source_id"] == "DOC_BASE"]
    assert len(acts) == 1

    # Deleting the document removes its data: the gap is back.
    assert client.delete(f"/api/v1/documents/{document['document_id']}").status_code == 200
    assert "acts" in _gap_keys(_well(client, "DEMO-002"))


@needs_db
def test_scanned_act_goes_through_ocr(client, generated, fake):
    document = _upload(client, generated / "documents" / "akt_demontazha_DEMO-005_scan.jpg")
    assert document["status"] == "loaded", document["status_message"]
    assert document["text_method"] == "image_ocr"
    assert any(call["kind"] == "ocr" and call["model"] == "fake-ocr" for call in fake.calls)
    well = _well(client, "DEMO-005")
    assert well["demount_act"] is True and well["demount_corrosion"] is True
    assert "acts" in _gap_keys(well)          # a fresh demount without a base act


@needs_db
def test_passport_sets_equipment_and_protection(client, generated):
    document = _upload(client, generated / "documents" / "pasport_DEMO-011.pdf")
    assert document["status"] == "loaded", document["status_message"]
    well = _well(client, "DEMO-011")
    assert "покрытием" in well["equipment"]
    assert "защитное исполнение НКТ" in well["protection_means"]


@needs_db
def test_memo_is_not_recognised_and_loads_nothing(client, generated):
    document = _upload(client, generated / "documents" / "sluzhebnaya_zapiska.pdf")
    assert document["status"] == "not_recognized"
    assert document["status_label"] == "Не распознан"
    assert not document.get("loaded")


@needs_db
def test_invented_values_are_not_loaded(client, generated, fake):
    original = fake.respond

    def hallucinating(body):
        answer = original(body)
        return answer.replace("DEMO-002", "DEMO-001").replace("язвенная", "общая")

    fake.respond = hallucinating
    document = _upload(client, generated / "documents" / "akt_razbora_DEMO-002.pdf")
    assert document["status"] == "not_recognized"
    assert "acts" in _gap_keys(_well(client, "DEMO-002"))


@needs_db
def test_inbox_folder(client, generated):
    from app.config import settings

    inbox = TEST_TMP / "inbox"
    target = inbox / "protokol.xlsx"
    shutil.copy(generated / "documents" / "protokol_khim_analiza_15.xlsx", target)
    old = time.time() - 60
    os.utime(target, (old, old))
    (inbox / "notes.txt").write_text("not a document")
    os.utime(inbox / "notes.txt", (old, old))
    summary = client.post("/api/v1/documents/inbox/scan").json()
    assert summary["accepted"] == 1 and summary["rejected"] == 1
    assert not target.exists()
    assert list((inbox / "processed").rglob("protokol.xlsx"))
    assert list((inbox / "failed").rglob("notes.txt.error.txt"))
    processed = client.post("/api/v1/documents/process-queue").json()["documents"]
    assert [item["status"] for item in processed] == ["loaded"]
    assert settings.docs_inbox_dir


@needs_db
def test_ocr_unavailable_is_retried_then_failed(client, generated, fake, monkeypatch):
    from app import db
    from app.config import settings
    from app.core import llm

    def broken(messages, **kwargs):
        if isinstance(messages[-1]["content"], list):
            raise llm.LLMError("нет соединения с сервером модели (ConnectionError).")
        return fake.chat(messages, **kwargs)

    monkeypatch.setattr(llm, "chat", broken)
    document = _upload(client, generated / "documents" / "akt_demontazha_DEMO-005_scan.jpg")
    assert document["status"] == "queued" and "Повтор" in document["status_message"]
    for _ in range(settings.docs_max_attempts - 1):
        db.update_document(document["document_id"], started_at=datetime.now(timezone.utc) - timedelta(minutes=5))
        db.claim_next_document()
        from app.core.documents.pipeline import process_document
        row = process_document(document["document_id"])
    assert row["status"] == "failed"
    assert "OCR" in row["status_message"]


# --------------------------------------------------------------------------- card fields of any document
def _pdf(path, lines):
    from scripts.build_demo_documents import _font_path, write_pdf

    write_pdf(path, lines, _font_path())
    return path


@needs_db
def test_protocol_pdf_keeps_tonnage_and_noo(client, tmp_path):
    # A lab protocol has no tonnage field of its own: the card fields of every type keep it.
    path = _pdf(tmp_path / "protokol.pdf", [
        "ПРОТОКОЛ ХИМИЧЕСКОГО АНАЛИЗА № 7", "",
        "Скважина | Дата отбора | Обводнённость, % | Хлориды, мг/л | Тоннаж, т | НОО, сут",
        f"DEMO-012 | {TODAY:%d.%m.%Y} | 44 | 21500 | 7.5 | 41"])
    document = _upload(client, path)
    assert document["status"] == "loaded", document["status_message"]
    assert document["doc_type"] == "lab_protocol"
    well = _well(client, "DEMO-012")
    assert well["tonnage_t"] == 7.5 and well["noo_days"] == 41
    assert well["provenance"]["tonnage_t"]["source"] == "Протокол хим. анализа (документ)"


@needs_db
def test_document_without_template_fills_the_card(client, tmp_path, fake):
    path = _pdf(tmp_path / "spravka.pdf", [
        "СПРАВКА О РАБОТЕ СКВАЖИНЫ", "", "Скважина № DEMO-011", f"Дата замера: {TODAY:%d.%m.%Y}",
        "Дебит жидкости: 63,5 м3/сут", "Тоннаж: 2,5 т"])
    document = _upload(client, path)
    assert document["status"] == "loaded", document["status_message"]
    assert document["doc_type"] == "well_document"
    assert any(call["kind"] == "classify" for call in fake.calls)       # no template matched
    detail = client.get(f"/api/v1/documents/{document['document_id']}").json()
    assert detail["extraction"]["type_method"] == "fallback"
    well = _well(client, "DEMO-011")
    assert well["liquid_rate_m3_day"] == 63.5 and well["tonnage_t"] == 2.5
    assert well["provenance"]["tonnage_t"]["source"] == "Документ по скважине (документ)"


def test_a_place_name_alone_is_no_measurement():
    from app.core.documents import load

    assert load._point({"field": "Южное"}, "2026-10-01", "DOC_DEMOUNT") is None
    point = load._point({"field": "Южное", "tonnage_t": 3.0}, "2026-10-01", "DOC_DEMOUNT")
    assert point.tonnage_t == 3.0 and point.field == "Южное"


@needs_db
def test_reupload_after_failure_processes_again(client, generated):
    from app import db

    path = generated / "documents" / "protokol_khim_analiza_15.xlsx"
    first = _upload(client, path)
    assert first["status"] == "loaded"
    # As if it had failed before (e.g. uploaded before the Excel reader existed).
    db.update_document(first["document_id"], status="failed", status_message="Языковая модель не настроена")
    with open(path, "rb") as handle:
        body = client.post("/api/v1/documents?wait=true", files=[("files", ("protocol khim analyzs.xlsx", handle))]).json()
    again = body["documents"][0]
    assert again["document_id"] == first["document_id"]
    assert again["status"] == "loaded" and again["requeued"] is True
    assert body["requeued"] == 1 and body["duplicates"] == 0
    # A loaded file sent again is just a duplicate.
    with open(path, "rb") as handle:
        body = client.post("/api/v1/documents?wait=true", files=[("files", (path.name, handle))]).json()
    assert body["duplicates"] == 1 and body["requeued"] == 0


@needs_db
def test_data_shared_by_two_documents_stays_until_both_are_deleted(client, tmp_path):
    from app import db
    from scripts.build_demo_documents import base_act_lines

    # The same act in two files: the event is stored once, under the first document.
    lines = base_act_lines(TODAY)
    act = _upload(client, _pdf(tmp_path / "akt.pdf", lines))
    act_copy = _upload(client, _pdf(tmp_path / "akt_kopiya.pdf", lines + ["Копия верна"]))
    assert act["status"] == act_copy["status"] == "loaded"
    acts = lambda: [event for event in db.load_history_events("DEMO-002") if event["source_id"] == "DOC_BASE"]  # noqa: E731
    assert len(acts()) == 1
    assert client.delete(f"/api/v1/documents/{act['document_id']}").status_code == 200
    assert len(acts()) == 1 and _well(client, "DEMO-002")["base_act"] is True
    assert client.delete(f"/api/v1/documents/{act_copy['document_id']}").status_code == 200
    assert acts() == [] and "acts" in _gap_keys(_well(client, "DEMO-002"))

    # The same sample in two protocols: it is stored under the last one.
    protocol = ["ПРОТОКОЛ ХИМИЧЕСКОГО АНАЛИЗА № 7", "",
                "Скважина | Дата отбора | Обводнённость, % | Хлориды, мг/л | Тоннаж, т | НОО, сут",
                f"DEMO-012 | {TODAY:%d.%m.%Y} | 44 | 21500 | 7.5 | 41"]
    _upload(client, _pdf(tmp_path / "protokol.pdf", protocol))
    latest = _upload(client, _pdf(tmp_path / "protokol_kopiya.pdf", protocol + ["Копия верна"]))
    assert client.delete(f"/api/v1/documents/{latest['document_id']}").status_code == 200
    well = _well(client, "DEMO-012")
    assert well["tonnage_t"] == 7.5
    assert well["provenance"]["tonnage_t"]["source"] == "Протокол хим. анализа (документ)"


def test_a_document_without_a_date_counts_from_the_day_it_was_received():
    from zoneinfo import ZoneInfo

    from app.config import settings
    from app.core.documents import load

    received = datetime(2026, 3, 1, 22, 30, tzinfo=timezone.utc)
    assert load._received_day({"created_at": received}) == \
        received.astimezone(ZoneInfo(settings.source_timezone)).date().isoformat()
    assert load._received_day({"created_at": received.isoformat()}) == load._received_day({"created_at": received})


def test_model_failures_are_explained_in_words():
    import requests

    from app.core import llm

    def http(status):
        response = requests.Response()
        response.status_code = status
        return requests.HTTPError(response=response)

    assert "нет соединения" in llm.describe_failure(requests.ConnectionError(), "m", 60)
    assert "MAI_OCR_API_URL" in llm.describe_failure(requests.ConnectionError(), "m", 60, "MAI_OCR_API_URL")
    assert "не ответила за 90 с" in llm.describe_failure(requests.ReadTimeout(), "m", 90)
    assert "ключ" in llm.describe_failure(http(401), "m", 60)
    assert "не найдены" in llm.describe_failure(http(404), "m", 60)
    assert "HTTP 503" in llm.describe_failure(http(503), "m", 60)
    assert "неожиданном формате" in llm.describe_failure(ValueError("no choices"), "m", 60)
    with pytest.raises(llm.LLMError, match="не заданы"):
        llm.chat([{"role": "user", "content": "?"}], model="", url="")
    assert FakeLiteLLM().chat([{"role": "user", "content": "Напиши слово: готово"}], model="fake") == "готово"


@needs_db
def test_clear_document_list(client, generated):
    memo = _upload(client, generated / "documents" / "sluzhebnaya_zapiska.pdf")
    protocol = _upload(client, generated / "documents" / "protokol_khim_analiza_15.xlsx")
    assert memo["status"] == "not_recognized" and protocol["status"] == "loaded"
    assert client.delete("/api/v1/documents").status_code == 422
    result = client.delete("/api/v1/documents?status=failed,not_recognized").json()
    assert result == {"deleted": 1, "wells": []}
    assert [row["document_id"] for row in client.get("/api/v1/documents").json()["documents"]] == [protocol["document_id"]]
    assert "sampling" not in _gap_keys(_well(client, "DEMO-012"))
    result = client.delete("/api/v1/documents?all=true").json()
    assert result["deleted"] == 1 and result["wells"] == ["DEMO-012", "DEMO-013"]
    assert client.get("/api/v1/documents").json()["documents"] == []
    assert "sampling" in _gap_keys(_well(client, "DEMO-012"))        # its data went with it
