"""Automatic results and admin-only fallback contracts, specified before implementation."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.schemas import TelemetryPoint
from tests.conftest import needs_db

NOW = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)


def run(records=(), payloads=None, **kwargs):
    from app.core.co2_automatic import assess, load_defaults
    return assess('AUTO-1', list(records), payloads or {}, defaults=load_defaults(), now=NOW, **kwargs)


def point(**values):
    return TelemetryPoint(timestamp=NOW, source_id='DARS', **values)


def test_empty_card_has_ready_result_with_explicit_project_defaults():
    result = run()
    assert result['automatic_status'] == 'calculated'
    assert result['scenarios'][0]['rate_mm_year'] > 0
    assert result['inputs']['ph_in_situ'] == 5
    assert result['inputs']['wall_shear_pa'] == 19
    assert result['input_provenance']['pressure_abs_bar']['kind'] == 'default'
    assert result['defaults_used'] and result['defaults_snapshot']['values']['co2_pct'] == 1.5
    assert result['calculation_mode'] == 'automatic' and result['state_at'] is None


def test_card_data_has_priority_over_defaults_and_is_not_overwritten():
    source = point(co2_pct=2, water_cut_pct=0, temperature_c=75, pressure_abs_bar=70,
                   ph_in_situ=4, wall_shear_pa=30, material='carbon_steel', water_wetting='water_wet')
    result = run([source])
    assert result['inputs']['temperature_c'] == 75
    assert result['inputs']['water_cut_pct'] == 0
    assert result['inputs']['co2_mole_fractions'] == [.02]
    assert result['input_provenance']['ph_in_situ']['kind'] == 'source'
    assert result['input_provenance']['ph_in_situ']['source_id'] == 'DARS'
    assert source.temperature_c == 75


def test_latest_value_is_selected_per_field_not_per_row():
    old = point(temperature_c=55, co2_pct=1)
    old.timestamp = NOW - timedelta(days=1)
    result = run([point(co2_pct=2), old])
    assert result['inputs']['temperature_c'] == 55
    assert result['inputs']['co2_mole_fractions'] == [.02]


def test_named_extra_measurements_in_card_are_usable_with_explicit_units():
    result = run([point(extra_measurements={'Абсолютное давление, бар': 80,
        'Температура, °C': 70, 'pH при рабочих условиях': 4.2, 'Касательное напряжение, Па': 25})])
    assert result['inputs']['pressure_abs_bar'] == 80
    assert result['inputs']['ph_in_situ'] == 4.2
    assert result['inputs']['wall_shear_pa'] == 25


def test_lab_ph_and_ambiguous_pressure_are_not_silently_relabelled():
    result = run([point(extra_measurements={'pH': 6.9, 'Давление, атм': 99})])
    assert result['inputs']['ph_in_situ'] == 5
    assert result['inputs']['pressure_abs_bar'] == 50
    assert result['input_provenance']['ph_in_situ']['kind'] == 'default'


def test_old_and_future_source_values_use_defaults_with_reasons():
    for moment in (NOW - timedelta(days=31), NOW + timedelta(days=2)):
        result = run([point(co2_pct=4).model_copy(update={'timestamp': moment})])
        assert result['inputs']['co2_mole_fractions'] == [.015]
        assert result['input_provenance']['co2_pct']['reason']


def test_nonfinite_source_values_are_serializable_and_do_not_get_a_plausible_rate():
    result = run([point().model_copy(update={'co2_pct':float('inf')})])
    assert result['automatic_status'] == 'not_applicable'
    assert result['scenarios'] == []
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('values', [dict(ph_in_situ=7), dict(co2_pct=0), dict(material='other'),
                                  dict(water_wetting='not_water_wet'), dict(h2s_partial_bar=.1)])
def test_inapplicable_real_values_are_not_replaced_by_convenient_defaults(values):
    result = run([point(**values)])
    assert result['automatic_status'] == 'not_applicable'
    assert result['scenarios'] == [] and result['quality_flags']
    assert result['configuration_hash'] and result['model_snapshot']


def test_admin_environment_changes_fallback_and_its_identity(monkeypatch):
    first = run()
    monkeypatch.setenv('MAI_CO2_DEFAULT_PH_IN_SITU', '4.5')
    second = run()
    assert second['inputs']['ph_in_situ'] == 4.5
    assert second['scenarios'][0]['rate_mm_year'] != first['scenarios'][0]['rate_mm_year']
    assert second['defaults_hash'] != first['defaults_hash']


def test_invalid_admin_defaults_fail_closed(monkeypatch):
    from app.core.co2_automatic import load_defaults
    from app.core.norsok import ModelConfigurationError
    monkeypatch.setenv('MAI_CO2_DEFAULT_PH_IN_SITU', '20')
    with pytest.raises(ModelConfigurationError): load_defaults()


def test_registry_wellhead_temperature_and_admin_confirmed_pressure_basis(monkeypatch):
    registry = {'DOC_NKT': {'date': NOW.isoformat(), 'rows': [{'wellhead_temp_c':70, 'wellhead_pressure_atm':60}]}}
    first = run(payloads=registry)
    assert first['inputs']['temperature_c'] == 70
    assert first['inputs']['pressure_abs_bar'] == 50
    monkeypatch.setenv('MAI_CO2_REGISTRY_PRESSURE_BASIS','gauge')
    second = run(payloads=registry)
    assert second['inputs']['pressure_abs_bar'] == pytest.approx(61*1.01325)
    assert second['input_provenance']['pressure_abs_bar']['kind'] == 'source'


def test_only_admin_can_allow_ambient_ph_as_an_explicit_assumption(monkeypatch):
    source = point(extra_measurements={'pH':4})
    assert run([source])['inputs']['ph_in_situ'] == 5
    monkeypatch.setenv('MAI_CO2_ALLOW_LAB_PH_AS_IN_SITU','true')
    result = run([source])
    assert result['inputs']['ph_in_situ'] == 4
    assert any('лабораторный pH' in flag for flag in result['quality_flags'])


@needs_db
def test_extra_parameters_from_two_sources_survive_merging(client):
    from app import db
    from app.db.co2 import automatic_inputs
    moment = datetime.now(timezone.utc)
    db.save_batch('EXTRAS-AUTO', None, [TelemetryPoint(timestamp=moment, source_id='DARS',
        extra_measurements={'Температура, °C':70})])
    db.save_batch('EXTRAS-AUTO', None, [TelemetryPoint(timestamp=moment, source_id='KHIMIK',
        extra_measurements={'pH при рабочих условиях':4})])
    data = automatic_inputs(['EXTRAS-AUTO'])['EXTRAS-AUTO']
    from app.core.co2_automatic import assess
    result = assess('EXTRAS-AUTO', **data)
    assert result['inputs']['temperature_c'] == 70 and result['inputs']['ph_in_situ'] == 4
    assert result['input_provenance']['ph_in_situ']['source_id'] == 'KHIMIK'


@needs_db
def test_bad_admin_config_has_an_explicit_status_and_preserves_other_card_fields(client, monkeypatch):
    before = client.get('/api/v1/rating/wells/DEMO-001').json()
    monkeypatch.setenv('MAI_CO2_DEFAULT_PH_IN_SITU','20')
    after = client.get('/api/v1/rating/wells/DEMO-001').json()
    assert after['co2_automatic']['automatic_status'] == 'configuration_error'
    assert after['co2_automatic']['scenarios'] == []
    assert after['index'] == before['index']


@needs_db
def test_old_ingestion_batch_identity_is_unchanged_when_new_fields_are_absent(client):
    from app import db
    from app.schemas import CO2_SOURCE_FIELDS
    from app.db.sources import _json_hash
    from app.db.schema import ingestion_batches
    from sqlalchemy import select
    source = point(co2_pct=2)
    result = db.save_batch('LEGACY-ID', 'legacy-auto-batch', [source])
    legacy_payload = {key:value for key,value in source.model_dump(mode='json').items()
                      if key not in CO2_SOURCE_FIELDS}
    with db.engine().connect() as conn:
        stored = conn.scalar(select(ingestion_batches.c.payload_sha256)
                             .where(ingestion_batches.c.batch_id == result['batch_id']))
    assert stored == _json_hash([legacy_payload])
    assert db.save_batch('LEGACY-ID', 'legacy-auto-batch', [source])['idempotent_replay']


@needs_db
def test_card_and_rating_return_ready_result_without_post_or_history_writes(client):
    from app.db.co2 import history
    before = history('DEMO-001')
    card = client.get('/api/v1/rating/wells/DEMO-001')
    assert card.status_code == 200
    result = card.json()['co2_automatic']
    assert result['automatic_status'] == 'calculated'
    assert result['input_provenance']['co2_pct']['kind'] == 'source'
    listed = client.get('/api/v1/rating').json()
    well = next(w for w in listed['wells'] if w['well_id'] == 'DEMO-001')
    assert well['co2_assessment']['scenarios'] == result['scenarios']
    assert history('DEMO-001') == before
    assert client.patch('/api/v1/rating/wells/DEMO-001', json={'ph_in_situ': 4}).status_code == 422


@needs_db
def test_source_update_changes_automatic_result_without_calculation_button(client):
    first = client.get('/api/v1/rating/wells/DEMO-001').json()['co2_automatic']
    body = {'well_id':'DEMO-001', 'source_id':'DARS', 'records':[{
        'timestamp':datetime.now(timezone.utc).isoformat(), 'co2_pct':2.1,
        'temperature_c':70, 'pressure_abs_bar':80, 'ph_in_situ':4, 'wall_shear_pa':30}]}
    response = client.post('/api/v1/ingestion/telemetry', json=body)
    assert response.status_code == 201, response.text
    second = client.get('/api/v1/rating/wells/DEMO-001').json()['co2_automatic']
    assert second['inputs']['ph_in_situ'] == 4
    assert second['input_hash'] != first['input_hash']


@needs_db
def test_automatic_loading_is_batched(client):
    from app.db import co2
    from app import db
    from sqlalchemy import event
    queries = []
    engine = db.engine()
    def capture(conn, cursor, statement, parameters, context, many): queries.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        data = co2.automatic_inputs([f'DEMO-{i:03}' for i in range(1, 14)])
    finally: event.remove(engine, 'before_cursor_execute', capture)
    assert len(data) == 13 and len(queries) <= 3
