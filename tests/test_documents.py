"""Document pipeline: checks without a database, then the full flow on PostgreSQL.

The full flow uploads the synthetic ДАРС / ХИМИК / ЭРА workbooks and the demo documents of
scripts/build_demo_documents.py, with LiteLLM replaced by tests.fake_litellm.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import date, timedelta

import pytest

from tests.conftest import ROOT, TEST_TMP
from tests.fake_litellm import FakeLiteLLM

from app.core.documents import extract, intake

TODAY = date.today()

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


def test_file_kinds():
    assert intake.detect_kind(b"%PDF-1.7 ...", "a.pdf") == "pdf"
    assert intake.detect_kind(b"\x89PNG\r\n\x1a\n....", "scan.png") == "image"
    with pytest.raises(intake.DocumentRejected):
        intake.detect_kind(b"\xd0\xcf\x11\xe0....", "old.xls")
    with pytest.raises(intake.DocumentRejected):
        intake.detect_kind(b"PK\x03\x04....", "letter.docx")


# --------------------------------------------------------------------------- PostgreSQL
needs_db = pytest.mark.skipif(not os.environ.get("MAI_TEST_DATABASE_URL"),
                              reason="set MAI_TEST_DATABASE_URL to run the PostgreSQL tests")


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_sample_data.py"), "--end-date", TODAY.isoformat(),
                    "--out", str(out)], check=True, capture_output=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_demo_documents.py"), "--end-date",
                    TODAY.isoformat(), "--out", str(out / "documents")], check=True, capture_output=True)
    return out


@pytest.fixture()
def fake(monkeypatch):
    from app.core import llm
    from scripts.build_demo_documents import scan_lines

    stub = FakeLiteLLM("\n".join(scan_lines(TODAY)))
    monkeypatch.setattr(llm, "chat", stub.chat)
    return stub


@pytest.fixture()
def client(generated, fake):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from app import db
    from app.main import app

    with TestClient(app) as test_client:
        with db.engine().begin() as conn:
            conn.execute(text("TRUNCATE documents, telemetry, ingestion_batches, history_events, history_batches, "
                              "source_health, source_documents, well_rating, analysis_runs, era_proposals CASCADE"))
        for profile, name in (("dars", "sample_dars.xlsx"), ("khimik", "sample_khimik.xlsx"), ("era", "sample_era.xlsx")):
            with open(generated / name, "rb") as handle:
                response = test_client.post("/api/v1/ingestion/excel", data={"profile_id": profile},
                                            files={"file": (name, handle)})
            assert response.status_code == 201, response.text
        yield test_client


def _upload(client, path, wait=True):
    with open(path, "rb") as handle:
        response = client.post(f"/api/v1/documents?wait={'true' if wait else 'false'}",
                               files=[("files", (path.name, handle))])
    assert response.status_code == 201, response.text
    return response.json()["documents"][0]


def _well(client, well_id):
    response = client.get(f"/api/v1/rating/wells/{well_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _gap_keys(well):
    return {item["key"] for item in well["data_gaps"]}


@needs_db
def test_base_act_pdf_closes_the_missing_act_gap(client, generated):
    assert "base_act" in _gap_keys(_well(client, "DEMO-002"))
    document = _upload(client, generated / "documents" / "akt_razbora_DEMO-002.pdf")
    assert document["status"] == "loaded", document["status_message"]
    assert document["doc_type"] == "base_inspection_act"
    assert document["text_method"] == "text_layer"
    assert document["well_ids"] == ["DEMO-002"]
    well = _well(client, "DEMO-002")
    assert "base_act" not in _gap_keys(well)
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
    assert "base_act" in _gap_keys(_well(client, "DEMO-002"))


@needs_db
def test_scanned_act_goes_through_ocr(client, generated, fake):
    document = _upload(client, generated / "documents" / "akt_demontazha_DEMO-005_scan.jpg")
    assert document["status"] == "loaded", document["status_message"]
    assert document["text_method"] == "image_ocr"
    assert any(call["kind"] == "ocr" and call["model"] == "fake-ocr" for call in fake.calls)
    well = _well(client, "DEMO-005")
    assert well["demount_act"] is True and well["demount_corrosion"] is True
    assert "base_act" in _gap_keys(well)          # a fresh demount without a base act


@needs_db
def test_lab_protocol_excel_form_counts_as_samples(client, generated):
    assert "samples" in _gap_keys(_well(client, "DEMO-012"))
    document = _upload(client, generated / "documents" / "protokol_khim_analiza_15.xlsx")
    assert document["status"] == "loaded", document["status_message"]
    assert document["doc_type"] == "lab_protocol"
    assert document["well_ids"] == ["DEMO-012", "DEMO-013"]
    well = _well(client, "DEMO-012")
    assert well["sampling"] is True
    assert "samples" not in _gap_keys(well)


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
    assert "base_act" in _gap_keys(_well(client, "DEMO-002"))


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
            raise llm.LLMError("LLM call failed (ConnectionError).")
        return fake.chat(messages, **kwargs)

    monkeypatch.setattr(llm, "chat", broken)
    document = _upload(client, generated / "documents" / "akt_demontazha_DEMO-005_scan.jpg")
    assert document["status"] == "queued" and "Повтор" in document["status_message"]
    for _ in range(settings.docs_max_attempts - 1):
        db.update_document(document["document_id"], started_at=db.datetime.now(db.timezone.utc) - timedelta(minutes=5))
        db.claim_next_document()
        from app.core.documents.pipeline import process_document
        row = process_document(document["document_id"])
    assert row["status"] == "failed"
    assert "OCR" in row["status_message"]


@needs_db
def test_ai_proposal_uses_rating_crews_and_reports_gaps(client, generated, fake):
    response = client.post("/api/v1/ingestion/crew-schedule?all_wells=true", json={"crew_availability": [
        {"crew_id": "КРС-7", "status": "available", "specialties": ["КРС"],
         "available_from": f"{TODAY.isoformat()}T08:00:00", "available_to": f"{(TODAY + timedelta(days=30)).isoformat()}T20:00:00"},
    ]})
    assert response.status_code == 201, response.text
    _upload(client, generated / "documents" / "sluzhebnaya_zapiska.pdf")
    proposal = client.post("/api/v1/rating/wells/DEMO-002/ai-proposal")
    assert proposal.status_code == 200, proposal.text
    body = proposal.json()
    well = _well(client, "DEMO-002")
    assert body["measure"] == well["measure_code"]
    assert body["crew"]["crew_id"] == "КРС-7"
    assert any("акта разбора" in item for item in body["missing_data"])
    assert well["ai_proposal"]["measure"] == body["measure"]
    assert "ai_proposal" not in client.get("/api/v1/rating").json()["wells"][0]

    # A crew that is not in the schedule is dropped, not invented.
    original = fake.respond
    fake.respond = lambda request: original(request).replace("КРС-7", "КРС-99")
    body = client.post("/api/v1/rating/wells/DEMO-002/ai-proposal").json()
    assert body["crew"] is None
    assert any("КРС-99" in note for note in body["checks"])


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
