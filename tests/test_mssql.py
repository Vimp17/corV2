"""SQL Server specifics of app/db.py: run with MAI_TEST_DATABASE_URL (see tests/conftest.py)."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MAI_TEST_DATABASE_URL"),
                                reason="set MAI_TEST_DATABASE_URL to run the SQL Server tests")


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


def test_start_checks_the_tables_and_creates_only_the_missing_ones(db):
    from sqlalchemy import text

    assert not any(db.init_db().values())                 # everything in place: nothing is changed
    with db.engine().begin() as conn:
        conn.execute(text(f"DROP TABLE {db.qualified('source_health')}"))
    assert db.init_db() == {"create": ["source_health"], "add": [], "drop": []}
    assert not any(db.init_db().values())


def test_a_login_that_only_reads_and_writes_starts_when_the_tables_exist(db, monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from app.config import settings

    login, password = f"mai_rw_{uuid.uuid4().hex[:8]}", f"Rw-{uuid.uuid4().hex}"
    schema = settings.database_schema or "dbo"
    admin = db.engine().execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"CREATE LOGIN [{login}] WITH PASSWORD = '{password}', CHECK_POLICY = OFF"))
        conn.execute(text(f"CREATE USER [{login}] FOR LOGIN [{login}] WITH DEFAULT_SCHEMA = [{schema}]"))
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[{schema}] TO [{login}]"))
    url = make_url(settings.database_url).set(username=login, password=password)
    restricted = create_engine(url, poolclass=NullPool, connect_args=db.connect_args(settings.database_url))
    if settings.database_schema:
        restricted = restricted.execution_options(schema_translate_map={None: settings.database_schema})
    monkeypatch.setattr(db, "_engine", restricted)
    try:
        assert not any(db.init_db().values())             # nothing to create: no right to create is needed
        with admin.connect() as conn:
            conn.execute(text(f"DROP TABLE {db.qualified('source_health')}"))
        with pytest.raises(RuntimeError, match="создать таблицы: source_health — ") as error:
            db.init_db()                                  # a table it cannot create is named
        assert "DB-Lib" not in str(error.value)           # the server's words, not the driver's wrapping
    finally:
        monkeypatch.undo()
        db.init_db()
        with admin.connect() as conn:
            conn.execute(text(f"DROP USER [{login}]"))
            conn.execute(text(f"DROP LOGIN [{login}]"))


def test_a_column_added_by_a_new_version_is_created(db):
    from sqlalchemy import inspect, text

    from app.config import settings

    with db.engine().begin() as conn:
        conn.execute(text(f"ALTER TABLE {db.qualified('well_rating')} DROP COLUMN ai_proposal_at"))
        conn.execute(text(f"ALTER TABLE {db.qualified('well_rating')} ADD analysis_id NVARCHAR(64) NULL"))
    db.init_db()
    with db.engine().connect() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns(
            "well_rating", schema=settings.database_schema or None)}
    assert "ai_proposal_at" in columns
    assert "analysis_id" not in columns     # a column of an earlier version is dropped


def test_a_deadlock_victim_is_run_again(db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(db.time, "sleep", lambda seconds: None)
    calls = []

    @db._retry_deadlocks
    def write():
        calls.append("write")
        if len(calls) < 3:
            raise OperationalError("MERGE …", {}, Exception(1205, b"Transaction was deadlocked on lock resources"))
        return "done"

    @db._retry_deadlocks
    def duplicate():
        calls.append("duplicate")
        raise OperationalError("INSERT …", {}, Exception(2627, b"Violation of UNIQUE KEY constraint"))

    assert write() == "done" and calls == ["write"] * 3
    calls.clear()
    with pytest.raises(OperationalError):
        duplicate()
    assert calls == ["duplicate"]                     # any other error is raised at once


def test_parallel_writes_of_new_wells_do_not_deadlock(db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from app.schemas import TelemetryPoint, WorkHistoryItem

    # No retries: writers of new wells at the same time must queue, not deadlock (MERGE … HOLDLOCK). Small
    # tables, as in a new database, are where the plans lock the widest key ranges.
    monkeypatch.setattr(db, "DEADLOCK_ATTEMPTS", 1)
    with db.engine().begin() as conn:
        for table in (db.telemetry, db.ingestion_batches, db.history_events, db.history_batches):
            conn.execute(table.delete())
    prefix = f"PAR-{uuid.uuid4().hex[:6]}"
    start = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)

    def push(index: int) -> str:
        well = f"{prefix}-{index:03d}"
        db.save_batch(well, None, [TelemetryPoint.model_validate({"timestamp": start - timedelta(days=day),
                                                                  "water_cut_pct": index + day, "source_id": "DARS"})
                                   for day in range(12)])
        db.save_history(well, None, [WorkHistoryItem(date=start + timedelta(days=day), work_type="Ремонт")
                                     for day in range(5)], [], source_id="ERA")
        return well

    with ThreadPoolExecutor(max_workers=8) as pool:
        wells = list(pool.map(push, range(100)))
    assert all(len(db.load_telemetry(well)) == 12 and len(db.load_history_events(well)) == 5 for well in wells)
