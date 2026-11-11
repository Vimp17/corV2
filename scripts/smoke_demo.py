"""Executed by the shipped portable Windows Python; no database or LLM required."""
import json
from pathlib import Path

import pyodbc
import pymssql
import pypdfium2
from PIL import Image
from app.main import app
from app.core.co2_corrosion import calculate
from app.core.co2_import import parse
from app.schemas import CO2Calculation

result = calculate(CO2Calculation(well_id='SMOKE', temperature_c=60, pressure_abs_bar=50))
assert len(result['scenarios']) == 3
assert result['scenarios'][1]['pco2_bar'] == .75
fixture = Path('examples/co2/v4-example.json')
assert parse(fixture.read_bytes(), fixture.name, True)['valid_rows'] == 1
assert '/api/v1/co2/calculate' in app.openapi()['paths']
print(json.dumps({'status':'ok', 'model':result['model_version'], 'scenarios':result['scenarios']}))
