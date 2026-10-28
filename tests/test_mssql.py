"""SQL Server specifics of the MSSQL mode (standalone/mssql_compat.py): run with a mssql+pyodbc
MAI_TEST_DATABASE_URL (see tests/conftest.py); skipped on PostgreSQL."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MAI_TEST_DATABASE_URL", "").startswith("mssql"),
                                reason="set a mssql+pyodbc MAI_TEST_DATABASE_URL to run the SQL Server tests")


@pytest.fixture(scope="module")
def db():
    from app import db

    db.init_db()
    return db


def _points(source_id: str, values: list[float]):
    from app.schemas import TelemetryPoint

    start = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    return [TelemetryPoint.model_validate({"timestamp": start + timedelta(days=index), "water_cut_pct": value,
                                          "source_id": source_id}) for index, value in enumerate(values)]


def test_well_ids_differing_in_case_stay_two_wells(db):
    upper = f"MSSQL-{uuid.uuid4().hex[:6]}".upper()
    lower = upper.lower()
    db.save_batch(upper, None, _points("DARS", [10, 11]))
    db.save_batch(lower, None, _points("DARS", [50]))
    assert [point.water_cut_pct for point in db.load_telemetry(upper)] == [10, 11]
    assert [point.water_cut_pct for point in db.load_telemetry(lower)] == [50]
    assert {upper, lower} <= set(db.known_well_ids())


def test_reloaded_rows_are_updated_and_known_events_skipped(db):
    from app.schemas import WorkHistoryItem

    well = f"MSSQL-{uuid.uuid4().hex[:6]}"
    db.save_batch(well, None, _points("DARS", [10, 11, 12]))
    db.save_batch(well, None, _points("DARS", [20, 21, 22]))           # the same moments, new values
    assert [point.water_cut_pct for point in db.load_telemetry(well)] == [20, 21, 22]

    act = WorkHistoryItem(date=datetime(2026, 9, 2, tzinfo=timezone.utc), work_type="Акт демонтажа")
    first = db.save_history(well, None, [act, act], [], source_id="ERA")  # the same event twice in one batch
    again = db.save_history(well, None, [act], [], source_id="ERA")
    assert first["work_records_stored"] == 1 and again["work_records_stored"] == 0
    assert len(db.load_history_events(well)) == 1
    replay = db.save_history(well, first["batch_id"], [act, act], [], source_id="ERA")
    assert replay["idempotent_replay"] is True


def test_application_lock_is_exclusive(db):
    key = 0x4D5353514C  # any key of this test
    with db.advisory_lock(key) as first:
        with db.advisory_lock(key) as second:
            assert first is True and second is False
    with db.advisory_lock(key) as again:
        assert again is True


def test_document_queue_takes_the_oldest_once(db):
    now = datetime.now(timezone.utc)
    ids = []
    for minutes in (2, 1):
        document_id = str(uuid.uuid4())
        db.create_document({"document_id": document_id, "sha256": uuid.uuid4().hex * 2, "filename": "a.pdf",
                            "channel": "upload", "file_kind": "pdf", "size_bytes": 1, "storage_path": "x/a.pdf",
                            "status": "queued", "attempts": 0, "created_at": now - timedelta(days=3650, minutes=minutes)})
        ids.append(document_id)
    assert db.claim_next_document()["document_id"] == ids[0]
    assert db.get_document(ids[0])["status"] == "processing"
    assert db.claim_next_document(ids[1])["document_id"] == ids[1]
    assert db.claim_next_document(ids[1]) is None                      # already taken
    for document_id in ids:
        db.delete_document_row(document_id)


def test_a_column_added_by_a_new_version_is_created(db):
    from sqlalchemy import inspect, text

    from standalone import mssql_compat

    with db.engine().begin() as conn:
        conn.execute(text(f"ALTER TABLE {mssql_compat.qualified('well_rating', conn.dialect)} DROP COLUMN ai_proposal_at"))
    db.init_db()
    with db.engine().connect() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns(
            "well_rating", schema=mssql_compat.installed_schema() or None)}
    assert "ai_proposal_at" in columns
