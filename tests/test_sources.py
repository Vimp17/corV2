"""Source data by API: pulled from MAI_SOURCE_<ID>_URL, fetched by an adapter, or pushed as JSON."""
from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

import pytest

import tests.conftest  # noqa: F401  (settings environment)

pytestmark = pytest.mark.skipif(not os.environ.get("MAI_TEST_DATABASE_URL"),
                                reason="set MAI_TEST_DATABASE_URL to run the SQL Server tests")
TODAY = date.today()
CREW = {"crew_id": "КРС-1", "specialties": ["КРС"], "status": "available"}


def _dars_book(days: int = 3) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Дата", "Обводнённость, %", "CO2, %"])
    for day in range(days):
        book.active.append([(TODAY - timedelta(days=day)).strftime("%d.%m.%Y"), 40 + day, 1.5])
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app

    with TestClient(app) as test_client:
        with db.engine().begin() as conn:
            for table in reversed(db.metadata.sorted_tables):
                if table.name != "monitor_jobs":
                    conn.execute(table.delete())
        yield test_client


@pytest.fixture()
def server():
    """A source API: /dars → a workbook, /crew → a schedule, /bad → not JSON, /empty → nothing,
    anything else → 404. Requests are kept for the checks."""
    seen: list[dict] = []
    answers = {"/dars": _dars_book(), "/crew": json.dumps({"crew_availability": [CREW]}).encode(),
               "/bad": b"not json", "/empty": b""}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            path, _, query = self.path.partition("?")
            seen.append({"path": path, "query": query, "authorization": self.headers.get("Authorization")})
            if path not in answers:
                self.send_response(404)
                self.end_headers()
                return
            body = answers[path]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    httpd.shutdown()


def _by_source(statuses: list[dict]) -> dict[str, dict]:
    return {row["source_id"]: row for row in statuses}


def test_pull_sources_of_a_well(client, server, monkeypatch):
    from app import db
    from app.core.source_connectors import refresh_sources_for_well

    base, seen = server
    monkeypatch.setenv("MAI_SOURCE_DARS_URL", f"{base}/dars")
    monkeypatch.setenv("MAI_SOURCE_DARS_TOKEN", "secret")
    monkeypatch.setenv("MAI_SOURCE_KHIMIK_URL", f"{base}/missing")
    monkeypatch.setenv("MAI_SOURCE_ERA_URL", "ftp://example/era")
    monkeypatch.setenv("MAI_SOURCE_CREW_SCHEDULE_URL", f"{base}/crew")
    statuses = _by_source(refresh_sources_for_well("PULL-1"))
    assert statuses["DARS"]["status"] == "ok" and statuses["DARS"]["record_count"] == 3
    assert statuses["DARS"]["origin"] == "api_refresh"
    assert statuses["KHIMIK"]["status"] == "unavailable" and "404" in statuses["KHIMIK"]["message"]
    assert statuses["ERA"]["status"] == "error" and statuses["ERA"]["message"] == "Configured source URL is invalid."
    assert statuses["CREW_SCHEDULE"]["status"] == "ok" and statuses["CREW_SCHEDULE"]["record_count"] == 1
    dars = next(item for item in seen if item["path"] == "/dars")
    assert dars["query"] == "well_id=PULL-1" and dars["authorization"] == "Bearer secret"
    assert len(db.load_telemetry("PULL-1")) == 3
    crews = db.load_source_payloads("PULL-1")["CREW_SCHEDULE"]["crew_availability"]
    assert crews[0]["crew_id"] == "КРС-1" and crews[0]["source_id"] == "CREW_SCHEDULE"

    # Not configured, an empty answer, an answer that does not match the profile.
    monkeypatch.delenv("MAI_SOURCE_DARS_URL")
    monkeypatch.setenv("MAI_SOURCE_KHIMIK_URL", f"{base}/empty")
    monkeypatch.setenv("MAI_SOURCE_CREW_SCHEDULE_URL", f"{base}/bad")
    statuses = _by_source(refresh_sources_for_well("PULL-1"))
    assert statuses["DARS"]["status"] == "not_configured" and "MAI_SOURCE_DARS_URL" in statuses["DARS"]["message"]
    assert statuses["KHIMIK"]["status"] == "unavailable" and "empty" in statuses["KHIMIK"]["message"]
    assert statuses["CREW_SCHEDULE"]["status"] == "error"
    assert statuses["CREW_SCHEDULE"]["message"] == "Source response did not match its profile (JSONDecodeError)."


def test_source_adapters(client, monkeypatch):
    from app import db
    from app.core import source_connectors

    class Adapter:
        def __init__(self, answer):
            self.answer = answer

        def fetch_for_well(self, well_id, profile):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    profiles = source_connectors.load_profiles()
    profiles = {
        "dars": {**profiles["dars"], "adapter": "workbook"},
        "khimik": {**profiles["khimik"], "adapter": "samples"},
        "era": {**profiles["era"], "adapter": "history"},
        "crew_schedule": {**profiles["crew_schedule"], "adapter": "crews"},
        "broken": {"source_id": "BROKEN", "kind": "telemetry", "field_map": {}, "adapter": "raises"},
        "absent": {"source_id": "ABSENT", "kind": "telemetry", "field_map": {}, "adapter": "not-installed"},
    }
    moment = f"{TODAY.isoformat()}T08:00:00+03:00"
    adapters = {
        "workbook": Adapter(_dars_book(2)),
        "samples": Adapter({"records": [{"timestamp": moment, "chlorides_mg_l": 21000, "tonnage_t": 3}]}),
        "history": Adapter({"work_history": [{"date": moment, "work_type": "Акт демонтажа", "corrosion_detected": True}]}),
        "crews": Adapter({"crew_availability": [CREW]}),
        "raises": Adapter(RuntimeError("down")),
    }
    monkeypatch.setattr(source_connectors, "load_profiles", lambda: profiles)
    monkeypatch.setattr(source_connectors, "_load_adapters", lambda: adapters)
    statuses = _by_source(source_connectors.refresh_sources_for_well("ADAPT-1"))
    assert statuses["DARS"]["status"] == "ok" and statuses["DARS"]["record_count"] == 2
    assert statuses["DARS"]["origin"] == "adapter"
    assert statuses["KHIMIK"]["status"] == "ok" and statuses["KHIMIK"]["record_count"] == 1
    assert statuses["ERA"]["status"] == "ok" and statuses["ERA"]["record_count"] == 1
    assert statuses["CREW_SCHEDULE"]["status"] == "ok"
    assert statuses["BROKEN"]["status"] == "error" and statuses["BROKEN"]["message"] == "Source adapter failed (RuntimeError)."
    assert statuses["ABSENT"]["status"] == "not_configured" and "not-installed" in statuses["ABSENT"]["message"]
    points = db.load_telemetry("ADAPT-1")
    assert len(points) == 3 and any(point.tonnage_t == 3 for point in points)
    assert db.load_history_events("ADAPT-1")[0]["payload"]["corrosion_detected"] is True
    assert db.load_source_payloads("ADAPT-1")["CREW_SCHEDULE"]["crew_availability"][0]["crew_id"] == "КРС-1"
    modes = {item["source_id"]: item["mode"] for item in source_connectors.source_modes()}
    assert modes["DARS"] == "adapter" and modes["ABSENT"] == "adapter_missing"


def test_push_json(client):
    moment = f"{TODAY.isoformat()}T08:00:00+03:00"
    body = {"well_id": "PUSH-1", "source_id": "dars", "batch_id": "B-1",
            "records": [{"timestamp": moment, "water_cut_pct": 71.4, "co2_pct": 0.8}]}
    first = client.post("/api/v1/ingestion/telemetry", json=body)
    assert first.status_code == 201, first.text
    result = first.json()
    assert result["rows_accepted"] == 1 and result["source_id"] == "DARS"
    assert result["source_statuses"][0]["origin"] == "api_push" and result["rating"]["mode"] == "sync"
    assert client.post("/api/v1/ingestion/telemetry", json=body).json()["idempotent_replay"] is True
    changed = {**body, "records": [{"timestamp": moment, "water_cut_pct": 50}]}
    assert client.post("/api/v1/ingestion/telemetry", json=changed).status_code == 409
    assert client.post("/api/v1/ingestion/telemetry", json={**body, "source_id": "ERA"}).status_code == 422
    assert client.get("/api/v1/ingestion/wells/PUSH-1/telemetry").json()["records"][0]["water_cut_pct"] == 71.4

    history = client.post("/api/v1/ingestion/history", json={
        "well_id": "PUSH-1", "work_history": [{"date": moment, "work_type": "Разбор на базе", "corrosion_detected": True}]})
    assert history.status_code == 201, history.text
    assert history.json()["rows_accepted"] == 1 and history.json()["source_id"] == "ERA"
    assert client.get("/api/v1/ingestion/wells/PUSH-1/history").json()["work_history"][0]["work_type"] == "Разбор на базе"
    assert client.post("/api/v1/ingestion/history", json={"well_id": "PUSH-1"}).status_code == 422

    crews = client.post("/api/v1/ingestion/crew-schedule?well_id=PUSH-1",
                        json={"crew_availability": [CREW, {"status": "available"}]})
    assert crews.status_code == 201, crews.text
    assert crews.json()["status"] == "partial" and crews.json()["rows_rejected"] == 1
    assert crews.json()["targets"] == ["PUSH-1"] and "1 crew records accepted, 1 rejected" in crews.json()["message"]
    assert client.post("/api/v1/ingestion/crew-schedule?well_id=PUSH-1", json={"crew_availability": []}).status_code == 422
    assert client.post("/api/v1/ingestion/crew-schedule?well_id=PUSH-1",
                       json={"crew_availability": [{"status": "available"}]}).status_code == 422
    assert client.post("/api/v1/ingestion/crew-schedule", json={"crew_availability": [CREW]}).status_code == 422
    everywhere = client.post("/api/v1/ingestion/crew-schedule?all_wells=true", json={"records": [CREW]}).json()
    assert everywhere["status"] == "ok" and everywhere["targets"] == ["PUSH-1"]


def test_liquid_rate_headers():
    from app.core.documents import nkt_registry
    from app.core.excel_cells import field_for

    for header in ("Средний дебит по жидкости, м3/сут", "Дебит жидкости, м3/сут", "Qж, м3/сут", "Дебит, м3/сут"):
        assert field_for(header, {}) == "liquid_rate_m3_day", header
        assert nkt_registry.column_key(header, set()) == "liquid_rate_m3_day", header
    for header in ("Дебит по нефти, т/сут", "Дебит газа, м3/сут"):       # not the liquid rate
        assert field_for(header, {}) is None and nkt_registry.column_key(header, set()) is None, header


def test_export_with_liquid_rate_by_its_name_fills_liquid_and_oil(client):
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Скважина", "Дата", "Средний дебит по жидкости, м3/сут", "Средняя обводненность, %"])
    book.active.append(["LIQ-1", TODAY.strftime("%d.%m.%Y"), 45.5, 70])
    buffer = BytesIO()
    book.save(buffer)
    result = client.post("/api/v1/ingestion/excel", data={"profile_id": "dars"},
                         files={"file": ("dars.xlsx", buffer.getvalue())}).json()
    assert result["source_columns"]["liquid_rate_m3_day"] == "Средний дебит по жидкости, м3/сут"
    assert result["extra_columns"] == []
    well = client.get("/api/v1/rating/wells/LIQ-1").json()
    assert well["liquid_rate_m3_day"] == 45.5 and well["oil_pct"] == 30 and well["oil_l_day"] == 13650
    # Among documents the same table is a ДАРС export: the liquid rate is not a ХИМИК field.
    from app.core.documents.text import detect_profile

    assert detect_profile(buffer.getvalue(), "liquid.xlsx") == "dars"


def test_a_file_imported_again_replaces_its_rows(client):
    from datetime import datetime, timezone

    from app import db
    from app.schemas import TelemetryPoint, WorkHistoryItem

    def points(*values):
        return [TelemetryPoint.model_validate({"timestamp": datetime(2026, 10, day, 8, tzinfo=timezone.utc),
                                               "source_id": "DARS", "water_cut_pct": value})
                for day, value in enumerate(values, start=1)]

    db.save_batch("REP-1", "FILE-1", points(10, 11, 12), replace=True)
    replayed = db.save_batch("REP-1", "FILE-1", points(10, 11, 12), replace=True)
    assert replayed["idempotent_replay"] is True
    db.save_batch("REP-1", "FILE-1", points(20), replace=True)          # the same file read differently
    assert [point.water_cut_pct for point in db.load_telemetry("REP-1")] == [20]
    with pytest.raises(ValueError):                                     # a pushed batch id stays strict
        db.save_batch("REP-1", "FILE-1", points(30))
    act = WorkHistoryItem(date=datetime(2026, 10, 1, tzinfo=timezone.utc), work_type="Акт демонтажа")
    db.save_history("REP-1", "FILE-2", [act, act.model_copy(update={"corrosion_detected": True})], [], source_id="ERA",
                    replace=True)
    db.save_history("REP-1", "FILE-2", [act], [], source_id="ERA", replace=True)
    assert len(db.load_history_events("REP-1")) == 1
