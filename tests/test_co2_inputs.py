"""Raw data contracts specified before the new batch calculator."""
import json
from io import BytesIO

import pytest
from openpyxl import Workbook

from tests.conftest import needs_db


def raw(**values):
    return dict(well_id='RAW-001', temperature_c=60, pressure_abs_bar=100,
                co2_pct=1, ph_in_situ=4, wall_shear_pa=19, material='carbon_steel',
                water_wetting='water_wet', state_at='2026-11-16T09:00:00+03:00') | values


def parse(rows):
    from app.core.co2_inputs import parse
    return parse(json.dumps(rows).encode(), 'inputs.json', False)


def test_raw_values_are_calculated_and_provenance_is_preserved():
    from app.core.co2_corrosion import calculate
    from tests.test_norsok import state
    report = parse([raw()])
    assert report['valid_rows'] == 1 and report['errors'] == []
    assessment = report['assessments'][0]
    assert assessment['scenarios'] == calculate(state())['scenarios']
    assert assessment['source_hash'] == report['source_hash']
    assert assessment['source_row'] == 2 and assessment['source_name'] == 'inputs.json'
    from datetime import datetime
    assert datetime.fromisoformat(assessment['inputs']['state_at']).isoformat() == '2026-11-16T06:00:00+00:00'


@pytest.mark.parametrize('changes', [dict(ph_in_situ=None), dict(wall_shear_pa=None),
    dict(pressure_abs_bar=None), dict(water_wetting='unknown'), dict(co2_pct=100),
    dict(ph_in_situ=7), dict(co2_pct=True)])
def test_missing_or_inapplicable_data_produces_row_diagnostics(changes):
    report = parse([raw(**changes)])
    assert report['valid_rows'] == 0 and report['errors'][0]['row'] == 2


def test_ambiguous_units_and_unknown_columns_are_not_silently_ignored():
    for extra in (dict(P_bar=100), dict(pH_measured=4), dict(co2_mole_fraction=.01)):
        report = parse([raw(**extra)])
        assert report['errors'] and not report['assessments']


def test_csv_decimal_comma_and_xlsx_have_the_same_result():
    from app.core.co2_inputs import parse as read
    values = raw()
    csv = ';'.join(values) + '\n' + ';'.join(str(v).replace('.', ',') for v in values.values())
    a = read(csv.encode('utf-8-sig'), 'data.csv', False)
    book = Workbook(); sheet = book.active
    sheet.append(list(values)); sheet.append(list(values.values()))
    content = BytesIO(); book.save(content)
    b = read(content.getvalue(), 'data.xlsx', False)
    assert not a['errors'] and not b['errors']
    assert a['assessments'][0]['scenarios'] == b['assessments'][0]['scenarios']


def test_duplicate_columns_and_empty_files_are_rejected():
    from app.core.co2_inputs import parse as read
    for content, filename in ((b'well_id;well_id\nA;B', 'x.csv'), (b'[]', 'x.json')):
        with pytest.raises(ValueError): read(content, filename, False)


def test_xlsx_formulas_cannot_be_mistaken_for_measured_numbers():
    from app.core.co2_inputs import parse as read
    row = raw(ph_in_situ='=2+2')
    book = Workbook(); book.active.append(list(row)); book.active.append(list(row.values()))
    content = BytesIO(); book.save(content)
    report = read(content.getvalue(), 'formulas.xlsx', False)
    assert not report['assessments'] and 'формул' in report['errors'][0]['message']


@needs_db
def test_bad_configuration_is_503_and_does_not_save(client, monkeypatch, tmp_path):
    from dataclasses import replace
    from app.core import norsok
    broken = tmp_path/'missing.json'
    monkeypatch.setattr(norsok, 'settings', replace(norsok.settings, co2_model_path=broken))
    assert client.get('/api/v1/co2/model').status_code == 503
    assert client.post('/api/v1/co2/calculate-file', files={'file': ('inputs.json', json.dumps([raw()]))}).status_code == 503
    from tests.test_norsok import state
    assert client.post('/api/v1/co2/calculate', json=state().model_dump(mode='json')).status_code == 503
    assert client.get('/api/v1/co2/wells/RAW-001').json()['assessments'] == []


@needs_db
def test_api_preview_and_atomic_confirm(client):
    files = {'file': ('inputs.json', json.dumps([raw()]))}
    url = '/api/v1/co2/calculate-file'
    metadata = client.get('/api/v1/co2/model')
    assert metadata.status_code == 200
    preview = client.post(url, files=files)
    assert preview.status_code == 200, preview.text
    report = preview.json()
    assert report['unknown_wells'] == ['RAW-001'] and not report['confirmed']
    assert client.get('/api/v1/co2/wells/RAW-001').json()['assessments'] == []
    data = dict(confirm='true', create_wells='true', configuration_hash=report['configuration_hash'])
    invalid = {'file': ('inputs.json', json.dumps([raw(), raw(well_id='RAW-BAD', ph_in_situ=None)]))}
    assert client.post(url, files=invalid, data=data).status_code == 422
    assert client.get('/api/v1/co2/wells/RAW-001').json()['assessments'] == []
    stale = client.post(url, files=files, data=data | {'configuration_hash': 'old'})
    assert stale.status_code == 409
    saved = client.post(url, files=files, data=data)
    assert saved.status_code == 200, saved.text
    assert saved.json()['stored'] == 1
    assert client.post(url, files=files, data=data).json()['duplicates'] == 1
    assert client.get('/api/v1/rating/wells/RAW-001').json()['index'] is None
