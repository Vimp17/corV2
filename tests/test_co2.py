"""CO₂ regression tests: units, strict v4 boundaries, history and rating isolation."""
import json
import math
from datetime import datetime, timezone
from io import BytesIO

import pytest
from pydantic import ValidationError

from app.core.co2_corrosion import calculate, rate
from app.core.co2_import import parse
from app.schemas import CO2Calculation
from tests.conftest import needs_db


def request(**values):
    return CO2Calculation(well_id="CO2-TEST", temperature_c=60, pressure_abs_bar=50, **values)


def row():
    # Constructed public fixture, independent of the user's 692 rows.
    return {"№ скважины": "CO2-EXAMPLE", "T_C": 60, "P_bar": 50,
            "V_1975_median": rate(60, .75), "V_A1_median": 2.5, "V_A2_median": 3.5,
            "S_CO2_final": 90, "pH_estimated": 1}


def test_base_scenarios_units_and_zero():
    result = calculate(request())
    expected = 10 ** (5.8 - 1710 / 333.15 + .67 * math.log10(.75))
    assert result["scenarios"][1]["pco2_bar"] == .75
    assert result["scenarios"][1]["rate_mm_year"] == pytest.approx(expected)
    assert result["state_at"] is None
    assert rate(60, 0) == 0
    assert rate(60, 3) / rate(60, .5) == pytest.approx(6 ** .67)
    assert calculate(request())["input_hash"] == result["input_hash"]


@pytest.mark.parametrize("values", [{"co2_mole_fractions": [1.5]}, {"co2_mole_fractions": [True]},
    {"co2_mole_fractions": [float('nan')]}, {"co2_mole_fractions": [.01,.01]},
    {"composition_source": "measured"}])
def test_bad_composition(values):
    with pytest.raises(ValidationError):
        request(**values)


@pytest.mark.parametrize("temperature,pressure", [(-273.15,1),(20,0),(float('inf'),1),(20,float('nan'))])
def test_bad_physics(temperature,pressure):
    with pytest.raises(ValidationError):
        CO2Calculation(well_id="X", temperature_c=temperature, pressure_abs_bar=pressure)


def test_import_preserves_rates_scores_and_unknown_dates():
    from app.core.excel_cells import field_for
    report = parse(json.dumps([row()]).encode(), "v4.json", True)
    assert not report["errors"]
    a = report["assessments"][0]
    assert a["state_at"] is None and a["synthetic"] is True
    assert a["imported_results"]["V_A2_median"] == 3.5
    assert a["scenarios"] == []  # A2 never silently recomputed
    assert "co2_pct" not in a["inputs"]
    for header in ("S_CO2_final", "S_CO2_A1", "S_CO2_1975"):
        assert field_for(header, {}) is None
    assert field_for("CO2, %", {}) == "co2_pct"


def test_import_invalid_duplicate_and_xlsx():
    from openpyxl import Workbook
    bad = row() | {"V_A2_median": float('inf')}
    assert parse(json.dumps([bad]).encode(), "v4.json", True)["errors"]
    assert len(parse(json.dumps([row(),row()]).encode(), "v4.json", True)["errors"]) == 1
    book = Workbook(); book.active.title = "📊 Расчёт"
    book.active.append(list(row())); book.active.append(list(row().values()))
    out = BytesIO(); book.save(out)
    assert parse(out.getvalue(), "v4.xlsx", True)["valid_rows"] == 1


@needs_db
def test_api_history_idempotence_and_rating_isolation(client):
    from app import db
    from app.db.co2 import history
    well = "DEMO-012"
    before = client.get(f"/api/v1/rating/wells/{well}").json()
    body = request().model_dump(mode="json") | {"well_id": well}
    first = client.post('/api/v1/co2/calculate', json=body)
    assert first.status_code == 200, first.text
    assert first.json()["stored"] == 1
    again = client.post('/api/v1/co2/calculate', json=body)
    assert again.json()["duplicates"] == 1
    assert again.json()["assessment"] == first.json()["assessment"]
    assert len(history(well)) == 1
    after = client.get(f"/api/v1/rating/wells/{well}").json()
    for key in ("index", "model_math", "model_ntc", "measure", "deadline"):
        assert after[key] == before[key]
    assert after["co2_assessments"][0]["state_at"] is None
    from app.core.proposal_agent import build_context
    from app.core.proposal_language import for_model
    payload = for_model(build_context(well), [], [], 365)
    assert payload["CO₂-коррозия (дополнительный контекст, не вероятность отказа)"]
    export = client.get('/api/v1/rating/export.xlsx')
    assert export.status_code == 200
    from openpyxl import load_workbook
    wb = load_workbook(BytesIO(export.content))
    assert "CO₂: скорости, мм/год" in [c.value for c in wb['Рейтинг'][4]]


@needs_db
def test_import_preview_explicit_creation_and_replay(client):
    content = json.dumps([row()]).encode()
    files = {"file": ("v4.json", content, "application/json")}
    preview = client.post('/api/v1/co2/import', files=files)
    assert preview.status_code == 200
    assert preview.json()["unknown_wells"] == ["CO2-EXAMPLE"]
    assert client.get('/api/v1/co2/wells/CO2-EXAMPLE').json()["assessments"] == []
    denied = client.post('/api/v1/co2/import', files=files, data={"confirm": "true"})
    assert denied.status_code == 409
    data = {"confirm":"true", "create_wells":"true"}
    imported = client.post('/api/v1/co2/import', files=files, data=data)
    assert imported.status_code == 200, imported.text
    assert imported.json()["stored"] == 1
    assert client.post('/api/v1/co2/import', files=files, data=data).json()["duplicates"] == 1
    well = client.get('/api/v1/rating/wells/CO2-EXAMPLE').json()
    assert well['index'] is None and well['model_math'] is None


@needs_db
def test_feature_flag_and_api_key(client, monkeypatch):
    from app.config import settings
    from dataclasses import replace
    import app.api.routes.co2 as routes
    import app.main as main
    monkeypatch.setattr(routes, 'settings', replace(settings, co2_enabled=False))
    assert client.get('/api/v1/co2/wells/X').status_code == 404
    monkeypatch.setattr(routes, 'settings', replace(settings, co2_enabled=True))
    monkeypatch.setattr(main, 'settings', replace(settings, api_key='test-co2-secret'))
    assert client.get('/api/v1/co2/wells/X').status_code == 401
    assert client.get('/api/v1/co2/wells/X', headers={'X-API-Key':'test-co2-secret'}).status_code == 200
