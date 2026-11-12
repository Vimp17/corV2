"""Acceptance cases written before implementing the approved QA plan."""
from datetime import datetime,timedelta,timezone
from io import BytesIO
import hashlib,json
import pytest
from tests.conftest import _upload,_well,needs_db

@needs_db
def test_error_cleanup_preserves_last_accepted_version(client,generated,monkeypatch):
    from app import db
    from app.core.documents import intake
    doc=_upload(client,generated/'documents'/'akt_razbora_DEMO-002.pdf')
    monkeypatch.setattr(intake,'read_original',lambda d:b'QA unreadable PDF')
    r=client.post(f"/api/v1/documents/{doc['document_id']}/reprocess?wait=true")
    assert r.json()['status']=='failed' and _well(client,'DEMO-002')['base_act']
    cleaned=client.delete('/api/v1/documents?status=failed,not_recognized').json()
    assert cleaned['deleted']==0 and cleaned['skipped_with_data']==1
    assert db.get_document(doc['document_id']) and _well(client,'DEMO-002')['base_act']
    assert client.delete(f"/api/v1/documents/{doc['document_id']}").status_code==200
    assert not _well(client,'DEMO-002')['base_act']

@needs_db
def test_document_pagination_and_search_reach_oldest_file(client):
    from app import db
    from app.db.schema import documents
    now=datetime.now(timezone.utc)
    with db.engine().begin() as conn:
        conn.execute(documents.insert(),[{'document_id':f'00000000-0000-0000-0000-{i:012d}',
            'sha256':hashlib.sha256(str(i).encode()).hexdigest(),'filename':f'QA-history-{i:03d}.pdf',
            'channel':'upload','file_kind':'pdf','size_bytes':1,'storage_path':'qa/unused',
            'status':'not_recognized','attempts':1,'created_at':now-timedelta(seconds=i)} for i in range(201)])
    first=client.get('/api/v1/documents?limit=200').json()
    assert first['matching_count']==201 and len(first['documents'])==200
    second=client.get('/api/v1/documents',params={'limit':200,'cursor':first['next_cursor']}).json()
    assert len(second['documents'])==1 and second['next_cursor'] is None
    assert second['documents'][0]['filename']=='QA-history-200.pdf'
    assert len({d['document_id'] for d in first['documents']+second['documents']})==201
    filtered=client.get('/api/v1/documents',params={'q':'history-200'}).json()
    assert filtered['matching_count']==1 and len(filtered['documents'])==1

def test_source_modes_include_crew_schedule(monkeypatch):
    from app.core.source_connectors import source_modes
    monkeypatch.setenv('MAI_SOURCE_CREW_SCHEDULE_URL','https://example.test/schedule')
    crew=next((s for s in source_modes() if s['source_id']=='CREW_SCHEDULE'),None)
    assert crew and crew['mode']=='api' and crew['kind']=='json_schedule'

@needs_db
def test_source_preview_never_stores_data(client,generated,monkeypatch):
    from app import db
    from app.core import source_connectors as sc
    from sqlalchemy import select,func
    def sizes():
        with db.engine().connect() as conn:
            return [conn.scalar(select(func.count()).select_from(t)) for t in (db.ingestion_batches,db.telemetry,db.history_events,db.source_health)]
    monkeypatch.setenv('MAI_SOURCE_DARS_URL','https://example.test/dars')
    from tests.test_sources import _dars_book
    monkeypatch.setattr(sc,'_download',lambda *args:_dars_book())
    before=sizes();r=client.post('/api/v1/sources/dars/check',json={'well_id':'DEMO-001'})
    assert r.status_code==200,r.text
    body=r.json();assert body['status']=='ok' and body['record_count']>0 and body['source_columns']
    assert sizes()==before

@pytest.mark.parametrize('code,body,expected',[(401,b'{}','401'),(302,b'<html>login</html>','302'),
    (200,b'<html>login</html>','XLSX'),(200,b'{"records":[]}','XLSX')])
def test_source_diagnostics_explain_http_without_secrets(monkeypatch,code,body,expected):
    import requests
    from app.core import source_connectors as sc
    response=requests.Response();response.status_code=code;response._content=body;response._content_consumed=True
    response.url='https://example.test/data?secret=QA-SECRET'
    response.headers['Content-Type']='application/json' if body.startswith(b'{') else 'text/html'
    monkeypatch.setenv('MAI_SOURCE_DARS_URL',response.url);monkeypatch.setenv('MAI_SOURCE_DARS_TOKEN','QA-SECRET')
    monkeypatch.setattr(sc,'_get_with_retries',lambda *a,**kw:response)
    from app.core.source_connectors import check_source
    r=check_source('dars','DEMO-001')
    assert r['status']=='error' and expected in r['message']
    assert 'QA-SECRET' not in json.dumps(r)

def test_header_auto_matches_document_path(generated):
    from openpyxl import load_workbook
    from app.core.excel_ingestion import parse_excel
    book=load_workbook(generated/'sample_dars.xlsx');book.active.insert_rows(1,3)
    book.active.cell(1,1,'Учебная выгрузка');out=BytesIO();book.save(out)
    a=parse_excel(out.getvalue(),'sample_dars.xlsx','dars',auto_header=True)
    b=parse_excel(out.getvalue(),'sample_dars.xlsx','dars')
    assert a['rows_accepted']==b['rows_accepted']>0

@needs_db
def test_co2_preview_exposes_missing_verification_inputs(client):
    from tests.test_co2 import row
    r=client.post('/api/v1/co2/import',files={'file':('qa.json',json.dumps([row()]))})
    assert r.status_code==200
    b=r.json();assert b['warning_count']>0 and any('pCO2_mid' in x['message'] for x in b['warnings'])
    assert not b['confirmed']

def test_per_well_source_cannot_relabel_another_wells_rows(monkeypatch):
    from openpyxl import Workbook
    from tests.conftest import TODAY
    from app.core import source_connectors as sc
    book=Workbook();book.active.append(['Скважина','Дата','Обводнённость, %'])
    book.active.append(['QA-A',TODAY.strftime('%d.%m.%Y'),40])
    book.active.append(['QA-B',TODAY.strftime('%d.%m.%Y'),90])
    out=BytesIO();book.save(out)
    monkeypatch.setenv('MAI_SOURCE_DARS_URL','https://example.test/dars')
    monkeypatch.setattr(sc,'_download',lambda *args:out.getvalue())
    report=sc.check_source('dars','QA-A')
    assert report['status']=='error' and 'другой скважины' in report['message']
    writes=[]
    monkeypatch.setattr(sc.db,'save_batch',lambda *args,**kwargs:writes.append(args))
    monkeypatch.setattr(sc.db,'record_source_status',lambda s,w,status,message,*args,**kwargs:{'status':status,'message':message})
    result=sc._from_url('QA-A','dars',sc.load_profiles()['dars'],'DARS')
    assert result[0]['status']=='error' and not writes

def test_crew_diagnostics_name_invalid_fields_without_echoing_values(monkeypatch):
    from app.core import source_connectors as sc
    monkeypatch.setenv('MAI_SOURCE_CREW_SCHEDULE_URL','https://example.test/crew')
    monkeypatch.setattr(sc,'_download',lambda *args:json.dumps({'crew_availability':[
        {'crew_id':'QA-1','date':'PRIVATE-SOURCE-VALUE','status':'available'}]}).encode())
    report=sc.check_source('crew_schedule','QA-A')
    assert report['status']=='error' and 'строк' in report['message'] and 'date' in report['message']
    assert 'PRIVATE-SOURCE-VALUE' not in report['message'] and 'pydantic' not in report['message']
