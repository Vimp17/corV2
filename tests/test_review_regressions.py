"""Red-phase regressions for the senior review; application changes await plan approval."""
from datetime import date, timedelta

from app.core.proposal_agent import _check
from app.core.proposal_language import merge_gaps


def _context(crew):
    today = date(2026, 11, 11)
    return {"well_id": "REVIEW-1", "today": today.isoformat(),
            "rating": {"measure_code": "РИ", "index": 70,
                       "deadline": (today + timedelta(days=7)).isoformat(), "data_gaps": []},
            "crews": [crew], "acts": [], "documents": []}


def test_review_reject_busy_crew_even_when_id_is_known():
    context = _context({"crew_id": "CREW-1", "status": "busy",
                        "available_from": "2026-11-01", "available_to": "2026-11-30"})
    result = _check({"measure": "РИ", "crew": {"crew_id": "CREW-1", "date": "2026-11-12"}}, context)
    assert result["crew"] is None, "Known ID does not make a busy crew available"


def test_review_reject_crew_date_outside_window_and_rating_deadline():
    context = _context({"crew_id": "CREW-1", "status": "available",
                        "available_from": "2026-11-12", "available_to": "2026-11-15"})
    result = _check({"measure": "РИ", "crew": {"crew_id": "CREW-1", "date": "2027-01-01"}}, context)
    assert result["crew"] is None, "Crew date must fit both availability and the rating deadline"


def test_review_gap_deduplication_keeps_additional_missing_indicator():
    merged = merge_gaps(["Нет данных о CO₂"], ["Нет данных о CO₂ и хлоридах"])
    assert any("хлорид" in item for item in merged), "Deduplication must not erase a new missing indicator"


def test_review_windows_pid_must_belong_to_this_application(monkeypatch):
    import ctypes
    from ctypes import wintypes
    from standalone.cli import service

    class Kernel:
        def OpenProcess(self, *args):
            return 1

        def GetExitCodeProcess(self, handle, code):
            code._obj.value = 259
            return 1

        def QueryFullProcessImageNameW(self, handle, flags, buffer, size):
            buffer.value = r'C:\unrelated\python.exe'
            return 1

        def CloseHandle(self, handle):
            return 1

    for name, value in {'WINDOWS': True, 'ctypes': ctypes, 'wintypes': wintypes,
                        '_kernel32': Kernel(), '_PROCESS_QUERY': 0x1000, '_STILL_ACTIVE': 259}.items():
        monkeypatch.setattr(service, name, value, raising=False)
    assert service._alive(12345) is False, 'A reused PID of another Python must never be treated as this API'


def test_review_trend_requires_actual_measurements_not_forward_fills():
    from datetime import datetime, timezone
    from app.core.features import build_features
    from app.core.normalization import normalize_records
    from app.schemas import TelemetryPoint

    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    points = [TelemetryPoint(timestamp=start + timedelta(days=i), source_id='DARS', co2_pct=.5,
                             chlorides_mg_l=20000, water_cut_pct=40 if i == 0 else 60 if i == 1 else None)
              for i in range(8)]
    features = build_features(normalize_records(points, 7))
    assert features.trends_per_day['water_cut_pct'] is None, 'Two real measurements do not satisfy eight observations'


def test_review_persistence_uses_measurement_even_if_other_source_is_later():
    from datetime import datetime, timezone
    from app.core.normalization import normalize_records
    from app.core.rules import _persistent
    from app.schemas import TelemetryPoint

    start = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
    points = []
    for i in range(7):
        # All real water measurements are within the existing six-day cutoff.
        actual = start + timedelta(days=i)
        if i:
            actual -= timedelta(hours=1)
        points.append(TelemetryPoint(timestamp=actual, source_id='DARS', water_cut_pct=90))
        points.append(TelemetryPoint(timestamp=start + timedelta(days=i), source_id='KHIMIK', chlorides_mg_l=20000))
    normalized = normalize_records(sorted(points, key=lambda p:p.timestamp), 7)
    assert _persistent(normalized, 'water_cut_pct', lambda value:value >= 75)


from tests.conftest import _upload, needs_db


@needs_db
def test_review_failed_reprocessing_keeps_accepted_document_data(client, generated, monkeypatch):
    from app import db
    from app.core.documents import extract

    document = _upload(client, generated / 'documents' / 'akt_razbora_DEMO-002.pdf')
    before = [event for event in db.load_history_events('DEMO-002') if event['source_id'] == 'DOC_BASE']
    assert before

    def unavailable(*args, **kwargs):
        raise extract.ExtractionUnavailable('temporary outage')

    monkeypatch.setattr(extract, 'extract_records', unavailable)
    response = client.post(f"/api/v1/documents/{document['document_id']}/reprocess?wait=true")
    assert response.status_code == 200
    after = [event for event in db.load_history_events('DEMO-002') if event['source_id'] == 'DOC_BASE']
    assert after == before, 'An extraction outage must preserve the previously accepted document version'


@needs_db
def test_review_reprocessing_must_not_release_an_active_claim(client, generated):
    from app import db

    document = _upload(client, generated / 'documents' / 'akt_razbora_DEMO-002.pdf', wait=False)
    assert db.claim_next_document(document['document_id'])['status'] == 'processing'
    client.post(f"/api/v1/documents/{document['document_id']}/reprocess")
    assert db.claim_next_document(document['document_id']) is None, 'An active claim must remain exclusive'


@needs_db
def test_review_deleting_document_preserves_independent_manual_import(client, generated):
    from app import db

    before = [point.model_dump(mode='json') for point in db.load_telemetry('DEMO-002')]
    document = _upload(client, generated / 'sample_dars.xlsx')
    assert document['status'] == 'loaded'
    assert client.delete(f"/api/v1/documents/{document['document_id']}").status_code == 200
    after = [point.model_dump(mode='json') for point in db.load_telemetry('DEMO-002')]
    assert after == before, 'Deleting a document must not delete a separate manual source import'


def test_review_numeric_quote_does_not_allow_factor_100_for_chlorides():
    from app.core.documents.extract import value_agrees
    assert not value_agrees(215.0, 'number', 'Хлориды: 21500 мг/л', 'chlorides_mg_l')


def test_review_boolean_quote_does_not_reverse_corrosion_negation():
    from app.core.documents.extract import value_agrees
    assert not value_agrees(True, 'boolean', 'Коррозия не обнаружена', 'corrosion_detected')


def test_review_math_model_rejects_nonfinite_baseline_time(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    import pytest
    from app.core import math_model

    path = tmp_path / 'invalid-model.json'
    path.write_text(json.dumps({'features': ['co2_pct'], 'coefficients': {'co2_pct': 0},
        'means': {'co2_pct': 0}, 'medians': {'co2_pct': 0},
        'baseline_survival': [{'time_days': 'NaN', 'survival': .5}]}))
    monkeypatch.setattr(math_model, 'settings', replace(math_model.settings, math_model_path=path))
    with pytest.raises(math_model.Unavailable):
        math_model.predict({'co2_pct': 1}, {})


def test_review_build_fails_if_python_source_cannot_compile(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    import pytest

    location = Path(__file__).resolve().parents[1] / 'standalone/build/build_windows.py'
    spec = importlib.util.spec_from_file_location('review_windows_builder', location)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    for directory in ('runtime/Lib', 'app', 'standalone'):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / 'app/broken.py').write_text('def broken(:\n')
    # One worker preserves the actual compiler while avoiding multiprocessing in this fixture.
    actual_compile = builder.compileall.compile_dir
    monkeypatch.setattr(builder.compileall, 'compile_dir', lambda *args, **kwargs:
                        actual_compile(*args, **(kwargs | {'workers': 1})))
    with pytest.raises((RuntimeError, SystemExit)):
        builder.compile_bytecode(tmp_path)


@needs_db
def test_review_mixed_source_does_not_extend_dars_field_as_lab_sample(client):
    from datetime import datetime, timezone
    from app import db
    from app.core.features import build_features
    from app.core.normalization import normalize_records
    from app.schemas import TelemetryPoint

    well = 'REVIEW-MIXED'
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    db.save_batch(well, None, [TelemetryPoint(timestamp=start, source_id='DARS', water_cut_pct=95)] + [
        TelemetryPoint(timestamp=start + timedelta(days=i), source_id='DARS', co2_pct=.5, inhibitor_efficiency=.9)
        for i in range(1, 11)])
    db.save_batch(well, None, [TelemetryPoint(timestamp=start, source_id='KHIMIK', chlorides_mg_l=20000)])
    normalized = normalize_records(db.load_telemetry(well), 7, sample_fill_days=30, sample_sources={'KHIMIK'})
    assert build_features(normalized).current_values['water_cut_pct'] is None, 'A lab field must not prolong unrelated DARS telemetry'


@needs_db
def test_review_empty_schedule_can_clear_previously_available_crews(client):
    from app import db
    route = '/api/v1/ingestion/crew-schedule?well_id=DEMO-002'
    created = client.post(route, json={'crew_availability': [{'crew_id': 'REVIEW-CREW', 'status': 'available'}]})
    assert created.status_code == 201, created.text
    cleared = client.post(route, json={'crew_availability': []})
    assert cleared.status_code == 201, cleared.text
    assert all(not payload.get('crew_availability') for payload in db.load_source_payloads('DEMO-002').values())


def test_review_missing_injection_deviation_cannot_confirm_protection():
    from datetime import datetime, timezone
    from app.core.rating_builder import build_values
    from app.schemas import TelemetryPoint

    now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    points = [TelemetryPoint(timestamp=now - timedelta(days=i), source_id='DARS', water_cut_pct=90,
                             co2_pct=.5, inhibitor_efficiency=.9, corrosion_rate_mm_year=.05)
              for i in range(7, -1, -1)]
    values, _ = build_values(points, [], now=now)
    assert values['protection'] != 'effective', 'Unknown injection deviation must not confirm protection'
    assert values['model_ntc'] == values['model_ntc_raw'], 'Unknown protection must not discount the risk score'
