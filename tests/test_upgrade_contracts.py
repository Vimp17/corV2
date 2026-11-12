"""Positive and boundary contracts written before the approved upgrade."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import TODAY, _upload, _well, needs_db


def test_quote_keeps_correct_units_and_percentage_conversion():
    from app.core.documents.extract import value_agrees
    assert value_agrees(21500, 'number', 'Хлориды: 21500 мг/л', 'chlorides_mg_l')
    assert value_agrees(.85, 'number', 'Эффективность ингибитора 85%', 'inhibitor_efficiency')
    assert value_agrees(False, 'boolean', 'Коррозия не обнаружена', 'corrosion_detected')
    assert value_agrees(True, 'boolean', 'Обнаружена язвенная коррозия', 'corrosion_detected')


def test_rank_ties_keep_excel_competition_places():
    from app.core.rating import build_rating
    rows = [{'well_id': str(i), 'model_ntc': score} for i, score in enumerate((.8, .8, .5, None))]
    assert [r['rank'] for r in build_rating(rows)] == [1, 1, 3, None]


def test_valid_crew_is_kept_with_open_availability_bounds():
    from app.core.proposal_agent import _check
    from tests.test_review_regressions import _context
    result = _check({'measure': 'РИ', 'crew': {'crew_id': 'C', 'date': '2026-11-12'}},
                    _context({'crew_id': 'C', 'status': 'available'}))
    assert result['crew']['crew_id'] == 'C'


@needs_db
def test_successful_reprocess_recomputes_both_old_and_new_wells(client, generated, tmp_path, monkeypatch):
    from app.core.documents import intake
    from scripts.build_demo_documents import base_act_lines, write_pdf, _font_path
    original = _upload(client, generated / 'documents' / 'akt_razbora_DEMO-002.pdf')
    assert _well(client, 'DEMO-002')['base_act'] is True
    replacement = tmp_path / 'new-act.pdf'
    write_pdf(replacement, [line.replace('DEMO-002', 'DEMO-003') for line in base_act_lines(TODAY)], _font_path())
    monkeypatch.setattr(intake, 'read_original', lambda document: replacement.read_bytes())
    response = client.post(f"/api/v1/documents/{original['document_id']}/reprocess?wait=true")
    assert response.status_code == 200 and response.json()['status'] == 'loaded', response.text
    assert not _well(client, 'DEMO-002')['base_act']
    assert _well(client, 'DEMO-003')['base_act'] is True


@needs_db
def test_only_stale_claims_are_requeued_and_reclaimed_with_new_identity(client, generated):
    from app import db
    doc = _upload(client, generated / 'documents' / 'akt_razbora_DEMO-002.pdf', wait=False)
    first = db.claim_next_document(doc['document_id'])
    assert db.requeue_stale_documents() == 0
    db.update_document(doc['document_id'], started_at=datetime.now(timezone.utc)-timedelta(hours=1))
    assert db.requeue_stale_documents() == 1
    second = db.claim_next_document(doc['document_id'])
    assert first.get('claim_id') and first['claim_id'] != second.get('claim_id')


@needs_db
def test_document_replacement_rolls_back_data_and_metadata_together(client, generated):
    from sqlalchemy.exc import IntegrityError
    from app import db
    doc = _upload(client, generated / 'documents' / 'akt_razbora_DEMO-002.pdf')
    before = db.load_history_events('DEMO-002')
    db.reset_document(doc['document_id'])
    claimed = db.claim_next_document(doc['document_id'])
    with pytest.raises(IntegrityError):
        db.replace_document_data(claimed, [{'well_id': 'DEMO-003', 'source_id': None, 'data': {}}],
                                 loaded={'wells':['DEMO-003']}, status='loaded')
    assert db.load_history_events('DEMO-002') == before
    assert db.get_document(doc['document_id'])['loaded'] == doc['loaded']


@needs_db
def test_expired_document_worker_cannot_commit_after_new_claim(client, generated):
    from app import db
    doc = _upload(client, generated / 'documents' / 'akt_razbora_DEMO-002.pdf', wait=False)
    first = db.claim_next_document(doc['document_id'])
    db.update_document(doc['document_id'], started_at=datetime.now(timezone.utc)-timedelta(hours=1))
    db.requeue_stale_documents()
    current = db.claim_next_document(doc['document_id'])
    with pytest.raises(ValueError):
        db.replace_document_data(first, [], status='loaded', loaded={'wells':[]})
    assert db.get_document(doc['document_id'])['claim_id'] == current['claim_id']


@needs_db
def test_co2_bulk_import_uses_bounded_sql_round_trips(client):
    from sqlalchemy import event
    from app import db
    from app.db.co2 import save_assessments
    from app.core.co2_corrosion import calculate
    from app.schemas import CO2Calculation
    queries = []
    def capture(conn, cursor, statement, parameters, context, many):
        queries.append(statement)
    engine = db.engine()
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        result = save_assessments([calculate(CO2Calculation(well_id=f'BULK-{i}', temperature_c=60,
                                  pressure_abs_bar=50)) for i in range(20)], create_wells=True)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert result['stored'] == 20
    assert len(queries) <= 8, f'Expected batched SQL, got {len(queries)} round trips'


def test_co2_import_parsing_does_not_block_the_api_event_loop(monkeypatch):
    import threading
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes import co2 as routes
    from app import db
    from app.core import co2_import
    from tests.test_co2 import row
    main_loop = []
    parse_threads = []
    original = co2_import.parse
    async def capture_loop():
        main_loop.append(threading.get_ident())
    def parse(*args):
        parse_threads.append(threading.get_ident())
        return original(*args)
    monkeypatch.setattr(co2_import, 'parse', parse)
    monkeypatch.setattr(db, 'list_rating_rows', lambda: [])
    monkeypatch.setattr(routes.co2, 'known_wells', lambda ids: set(), raising=False)
    app = FastAPI(dependencies=[])
    from fastapi import Depends
    app.include_router(routes.router, dependencies=[Depends(capture_loop)])
    with TestClient(app) as client:
        response = client.post('/co2/import', files={'file':('v4.json', json.dumps([row()]))})
    assert response.status_code == 200, response.text
    assert main_loop[0] != parse_threads[0], 'A slow parser/SQL must run outside the API event loop'


@needs_db
def test_legacy_document_migration_is_repeatable_and_preserves_external_batches(client, generated):
    from app import db
    from app.db.schema import document_contributions
    from sqlalchemy import select, delete
    from app.schemas import WorkHistoryItem
    doc = _upload(client, generated/'documents'/'akt_razbora_DEMO-002.pdf',wait=False)
    key = f"doc-{doc['document_id']}-DEMO-002"
    work = WorkHistoryItem(date=datetime(2026,11,1,tzinfo=timezone.utc),work_type='Разбор на базе',corrosion_detected=True)
    db.save_history('DEMO-002', key, [work], [], source_id='DOC_BASE')
    db.update_document(doc['document_id'], status='loaded', loaded={'wells':['DEMO-002'],'source_id':'DOC_BASE'})
    db.init_db(); db.init_db()
    assert db.get_document(doc['document_id'])['loaded']['storage_version'] == 2
    assert len([e for e in db.load_history_events('DEMO-002') if e['source_id']=='DOC_BASE']) == 1
    assert client.delete(f"/api/v1/documents/{doc['document_id']}").status_code == 200
    assert not [e for e in db.load_history_events('DEMO-002') if e['source_id']=='DOC_BASE']
    assert db.load_telemetry('DEMO-002')


@needs_db
def test_well_statistics_include_document_owned_observations(client, generated):
    from app import db
    from app.db.schema import document_contributions
    doc = _upload(client, generated/'documents'/'akt_razbora_DEMO-002.pdf', wait=False)
    with db.engine().begin() as conn:
        conn.execute(document_contributions.insert().values(document_id=doc['document_id'],
            well_id='DOCUMENT-ONLY', source_id='DOC_LAB', updated_at=datetime.now(timezone.utc),
            data={'records':[{'timestamp':'2026-11-01T12:00:00+00:00','co2_pct':2}],
                  'work_history':[{'date':'2026-11-01T12:00:00+00:00','work_type':'Ремонт'}]}))
    row = next(row for row in db.list_wells() if row['well_id']=='DOCUMENT-ONLY')
    assert row['record_count'] == 1 and row['history_count'] == 1
    assert row['first_timestamp'] == row['last_timestamp'] == datetime(2026,11,1,12,tzinfo=timezone.utc)
