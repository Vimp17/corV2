"""Executed by the shipped portable Windows Python; no database or LLM required."""
import json
from pathlib import Path
import sys
base = Path(__file__).resolve().parent
sys.path.insert(0, str(base if (base/"app").is_dir() else base.parent))

import pyodbc
import pymssql
import pypdfium2
from PIL import Image
from app.main import app
from standalone.manage import parser
assert parser().parse_args(["status"]).command == "status"
assert parser().parse_args(["sources", "check", "--well", "SMOKE"]).action == "check"
from app.core.co2_corrosion import calculate
from app.core.co2_import import parse
from app.schemas import CO2Calculation

result = calculate(CO2Calculation(well_id='SMOKE', model_id='co2-v4-base-1', temperature_c=60,
                                pressure_abs_bar=50, co2_mole_fractions=[.005, .015, .03]))
assert len(result['scenarios']) == 3
assert result['scenarios'][1]['pco2_bar'] == .75
fixture = Path('examples/co2/v4-example.json')
assert parse(fixture.read_bytes(), fixture.name, True)['valid_rows'] == 1
assert '/api/v1/co2/calculate' in app.openapi()['paths']
from app.core.co2_inputs import parse as parse_inputs
from app.core.norsok import load_model, point_rate
import math
assert math.isclose(point_rate(60, 1, 19, 4, load_model()), 11.858616, rel_tol=1e-10)
result = calculate(CO2Calculation(well_id='NORSOK-SMOKE', temperature_c=60, pressure_abs_bar=100,
    co2_mole_fractions=[.01], composition_source='measured', ph_in_situ=4, wall_shear_pa=19,
    material='carbon_steel', water_wetting='water_wet'))
assert result['model_snapshot']['model_id'] == 'norsok-m506-2005'
assert result['configuration_hash']
from app.core.co2_automatic import assess
automatic = assess('AUTO-SMOKE', [], {})
assert automatic['automatic_status'] == 'calculated'
assert automatic['inputs']['ph_in_situ'] == 5
assert automatic['input_provenance']['ph_in_situ']['kind'] == 'default'
assert automatic['defaults_hash']
assert parse_inputs(Path('examples/co2/norsok-inputs.json').read_bytes(), 'norsok-inputs.json', True)['valid_rows'] == 1
assert '/api/v1/co2/calculate-file' in app.openapi()['paths']
assert '/api/v1/sources/{profile_id}/check' in app.openapi()['paths']
assert (base/'dashboard/sources.js').is_file()
print(json.dumps({'status':'ok', 'model':result['model_version'], 'scenarios':result['scenarios']}))

# Native CLI process control is exercised against an isolated local probe, not a production DB.
import socket
import subprocess
import tempfile
from standalone.cli import service
with tempfile.TemporaryDirectory(prefix='mai-cli-smoke-') as folder:
    root = Path(folder)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    subprocess.run([sys.executable, str(base/'examples/generate_sample_data.py'), '--out', str(root/'samples')], check=True)
    assert len(list((root/'samples').glob('sample_*.xlsx'))) == 3
    (root/'probe.py').write_text('''import os, hashlib
from pathlib import Path
from fastapi import FastAPI
app = FastAPI()
@app.get('/health/live')
def live():
    return {'pid':os.getpid(),'root':hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()}
@app.get('/health/ready')
def ready():
    return {'status':'ready'}
''', encoding='utf-8')
    values = {'MAI_DATABASE_URL':'mssql+pymssql://test:test@localhost/test',
              'MAI_STANDALONE_HOST':'127.0.0.1','MAI_STANDALONE_PORT':str(port)}
    service.ROOT = root
    service.PID_FILE = root/'api.pid'
    service.LOG_FILE = root/'api.log'
    service.DATA_DIRS = []
    service.ENTRY = 'probe:app'
    service.require_env = service.env_values = lambda: values
    try:
        assert service.cmd_start(None) == 0
        assert service.cmd_status(None) == 0
        assert service.cmd_restart(None) == 0
        service.PID_FILE.unlink()  # safe recovery must verify root and process identity
        assert service.cmd_stop(None) == 0
        assert not service._running(values)[1]
    finally:
        service.cmd_stop(None)
print('Native start/status/restart/stop and lost-PID recovery: OK')
