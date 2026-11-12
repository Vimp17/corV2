"""Acceptance contracts written before the approved follow-up implementation."""
import base64
import hashlib
import io
import json
import ssl
from pathlib import Path
import urllib.error

import pytest
from tests.conftest import needs_db, _upload, _well
from tests.test_co2 import row
from app.core.co2_import import parse


@pytest.mark.parametrize('changes', [
    {'pH_used':15}, {'pH_measured':-1}, {'pH_estimated':2}, {'S_CO2_final':101},
    {'pCO2_mid':51}, {'pCO2_low':2,'pCO2_high':1}, {'D_mm':0},
    {'pCO2_mid':.75,'V_1975_median':9}, {'S_CO2_A1':42},
    {'S_CO2_1975':100,'S_CO2_A1':100,'S_CO2_A2':100,'S_CO2_final':90},
])
def test_import_rejects_inconsistent_v4_values(changes):
    report=parse(json.dumps([row() | changes]).encode(),'v4.json',True)
    assert report['valid_rows']==0 and report['errors']


def test_import_reports_missing_input_for_formula_verification():
    report=parse(json.dumps([row()]).encode(),'v4.json',True)
    assert report['valid_rows']==1
    assert any('pCO2_mid' in flag for flag in report['assessments'][0]['quality_flags'])


def test_verified_import_and_zero_boundary():
    item=row() | {'pCO2_low':0,'pCO2_mid':.75,'pCO2_high':50,
        'S_CO2_1975':100,'S_CO2_A1':100,'S_CO2_A2':100,'S_CO2_final':100}
    assert parse(json.dumps([item]).encode(),'v4.json',True)['valid_rows']==1
    item.update(pCO2_mid=0,V_1975_median=0,S_CO2_1975=0,S_CO2_final=200/3)
    assert parse(json.dumps([item]).encode(),'v4.json',True)['valid_rows']==1


@needs_db
def test_failed_rating_is_retried_from_database_without_document_reprocessing(client,generated,monkeypatch):
    from app import db
    from app.core import rating_builder
    from app.core.documents import pipeline
    original=rating_builder.update
    def broken(well):
        raise RuntimeError('transient rating failure')
    monkeypatch.setattr(rating_builder,'update',broken)
    doc=_upload(client,generated/'documents'/'akt_razbora_DEMO-002.pdf')
    assert doc['status']=='loaded'
    assert not _well(client,'DEMO-002')['base_act']
    db.engine().dispose()  # Recovery must not depend on the worker's in-memory state.
    monkeypatch.setattr(rating_builder,'update',original)
    assert pipeline.process_queue()==[]
    assert _well(client,'DEMO-002')['base_act'] is True
    assert db.get_document(doc['document_id'])['attempts']==1


@needs_db
def test_migration_reads_only_legacy_metadata(client,monkeypatch):
    from app import db
    from sqlalchemy import event
    from app.db.documents import migrate_document_data
    statements=[]
    def capture(conn,cursor,statement,parameters,context,many): statements.append(statement)
    event.listen(db.engine(),'before_cursor_execute',capture)
    try:
        with db.engine().begin() as conn: migrate_document_data(conn)
    finally: event.remove(db.engine(),'before_cursor_execute',capture)
    assert len(statements)==1
    sql=statements[0].lower()
    assert 'where' in sql and 'documents.text' not in sql and 'documents.extraction' not in sql


def builder():
    import importlib.util
    spec=importlib.util.spec_from_file_location('builder',Path('standalone/build/build_windows.py'))
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_build_retries_timeout_without_accepting_partial_download(tmp_path,monkeypatch):
    m=builder();calls=[]
    def open_url(url,timeout):
        calls.append(timeout)
        if len(calls)==1: raise TimeoutError('IPv6 connect timeout')
        return io.BytesIO(b'verified response')
    monkeypatch.setattr(m.urllib.request,'urlopen',open_url)
    target=tmp_path/'metadata.json'
    m.download('https://example.test/metadata',target)
    assert target.read_bytes()==b'verified response' and len(calls)==2 and max(calls)<=30
    assert not list(tmp_path.glob('*.part'))


def test_build_reuses_official_hash_metadata_and_checks_package_each_time(tmp_path,monkeypatch):
    import zipfile
    m=builder(); monkeypatch.setattr(m,'CACHE',tmp_path/'cache')
    package=m.CACHE/'downloads'/f'python.{m.PYTHON_VERSION}.nupkg'
    package.parent.mkdir(parents=True)
    with zipfile.ZipFile(package,'w') as z: z.writestr('tools/python.exe',b'portable runtime fixture')
    digest=base64.b64encode(hashlib.sha512(package.read_bytes()).digest()).decode()
    calls=[]
    def open_url(url,timeout):
        calls.append(url)
        return io.BytesIO(json.dumps({'catalogEntry':{'packageHash':digest,'packageHashAlgorithm':'SHA512'}}).encode())
    monkeypatch.setattr(m.urllib.request,'urlopen',open_url)
    m.python_runtime(tmp_path/'one');m.python_runtime(tmp_path/'two')
    assert len(calls)==1
    with zipfile.ZipFile(package,'a') as z: z.writestr('tampered',b'bad')
    with pytest.raises(SystemExit,match='hash mismatch'):m.python_runtime(tmp_path/'three')
    assert not (tmp_path/'three/python.exe').exists()


@needs_db
def test_new_document_revision_is_not_acknowledged_by_an_older_recompute(client,generated,monkeypatch):
    from app import db
    from app.db.recompute import enqueue, pending
    from app.core import rating_builder
    from app.core.documents import pipeline
    _upload(client,generated/'documents'/'akt_razbora_DEMO-002.pdf')
    with db.engine().begin() as conn: enqueue(conn,['DEMO-002'])
    original=rating_builder.update
    def changed_while_computing(well):
        result=original(well)
        with db.engine().begin() as conn: enqueue(conn,[well])
        return result
    monkeypatch.setattr(rating_builder,'update',changed_while_computing)
    pipeline.process_queue()
    assert [job['well_id'] for job in pending()]==['DEMO-002']
    monkeypatch.setattr(rating_builder,'update',original)
    pipeline.process_queue()
    assert pending()==[]


@needs_db
def test_document_transaction_rolls_back_pending_recompute(client,generated):
    from app import db
    from app.db.recompute import pending
    from sqlalchemy import event
    doc=_upload(client,generated/'documents'/'akt_razbora_DEMO-002.pdf')
    db.reset_document(doc['document_id']);claim=db.claim_next_document(doc['document_id'])
    before=db.load_history_events('DEMO-002')
    def fail_after_queue_insert(conn,cursor,statement,parameters,context,many):
        if statement.startswith('MERGE') and 'rating_recompute_queue' in statement:
            raise RuntimeError('failure after the durable enqueue')
    event.listen(db.engine(),'after_cursor_execute',fail_after_queue_insert)
    try:
        with pytest.raises(RuntimeError,match='durable enqueue'):
            db.replace_document_data(claim,[{'well_id':'NEW','source_id':'DOC_BASE','data':{}}],
                                     status='loaded',loaded={'wells':['NEW']})
    finally:
        event.remove(db.engine(),'after_cursor_execute',fail_after_queue_insert)
    assert pending()==[]
    assert db.get_document(doc['document_id'])['loaded']==doc['loaded']
    assert db.load_history_events('DEMO-002')==before


@needs_db
def test_migration_handles_json_null_and_sql_null(client,generated):
    from app import db
    from sqlalchemy import update, null
    from app.db.schema import documents
    doc=_upload(client,generated/'documents'/'akt_razbora_DEMO-002.pdf',wait=False)
    for value in (None,null(),{}):
        with db.engine().begin() as conn:
            conn.execute(update(documents).where(documents.c.document_id==doc['document_id']).values(loaded=value))
        db.init_db()
    assert db.get_document(doc['document_id'])['status']=='queued'


@needs_db
def test_post_commit_status_failure_keeps_document_loaded_and_recompute_retry(client,generated,monkeypatch):
    from app import db
    from app.core.documents import pipeline
    original=db.record_source_status
    def fail_document_status(*args,**kwargs):
        if kwargs.get('origin')=='document': raise RuntimeError('status write unavailable')
        return original(*args,**kwargs)
    monkeypatch.setattr(db,'record_source_status',fail_document_status)
    doc=_upload(client,generated/'documents'/'akt_razbora_DEMO-002.pdf')
    assert doc['status']=='loaded'
    monkeypatch.setattr(db,'record_source_status',original)
    pipeline.process_queue()
    assert _well(client,'DEMO-002')['base_act'] is True


@pytest.mark.parametrize('error',[urllib.error.HTTPError('https://example.test',403,'Forbidden',{},None),
    urllib.error.URLError(ssl.SSLCertVerificationError('untrusted certificate'))])
def test_build_does_not_retry_access_or_certificate_failure(tmp_path,monkeypatch,error):
    m=builder();calls=[]
    def open_url(url,timeout): calls.append(url);raise error
    monkeypatch.setattr(m.urllib.request,'urlopen',open_url)
    with pytest.raises(type(error)):m.download('https://example.test',tmp_path/'package')
    assert len(calls)==1 and not list(tmp_path.iterdir())


def test_build_discards_partial_response_before_retry(tmp_path,monkeypatch):
    m=builder();calls=[]
    class Interrupted(io.BytesIO):
        def read(self,size=-1):
            if self.tell(): raise TimeoutError('transfer interrupted')
            return super().read(size)
    def open_url(url,timeout):
        calls.append(url)
        return Interrupted(b'partial') if len(calls)==1 else io.BytesIO(b'complete')
    monkeypatch.setattr(m.urllib.request,'urlopen',open_url)
    target=tmp_path/'package'
    m.download('https://example.test',target)
    assert target.read_bytes()==b'complete' and not list(tmp_path.glob('*.part'))


@needs_db
def test_startup_recovers_a_committed_rating_invalidation(client,generated,monkeypatch):
    import asyncio
    from app.core import rating_builder
    from app.main import app
    original=rating_builder.update
    monkeypatch.setattr(rating_builder,'update',lambda well:(_ for _ in ()).throw(RuntimeError('offline')))
    _upload(client,generated/'documents'/'akt_razbora_DEMO-002.pdf')
    assert not _well(client,'DEMO-002')['base_act']
    monkeypatch.setattr(rating_builder,'update',original)
    async def restart():
        async with app.router.lifespan_context(app): pass
    asyncio.run(restart())
    assert _well(client,'DEMO-002')['base_act'] is True


@needs_db
def test_retry_of_deleted_well_never_recreates_its_rating(client,monkeypatch):
    from app import db
    from app.db.recompute import enqueue,pending
    from app.core import rating_builder
    from app.core.documents import pipeline
    with db.engine().begin() as conn:enqueue(conn,['REMOVED-WELL'])
    monkeypatch.setattr(rating_builder,'update',lambda well:(_ for _ in ()).throw(AssertionError('must not recreate')))
    pipeline.process_queue()
    assert db.get_rating_row('REMOVED-WELL') is None and pending()==[]
