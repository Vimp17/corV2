"""Read a local v4 JSON; verify base reproduction without publishing the dataset."""
import argparse
import json
import math
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.co2_corrosion import rate
from app.core.co2_import import parse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('path', type=Path)
args = parser.parse_args()
content = args.path.read_bytes()
report = parse(content, args.path.name, True)
assert not report['errors'], report['errors']
rows = json.loads(content)
for row in rows:
    assert math.isclose(rate(row['T_C'], row['pCO2_mid']), row['V_1975_median'], rel_tol=1e-10)
print(json.dumps({'rows':len(rows), 'valid_import_rows':report['valid_rows'],
                  'base_formula_matches':len(rows), 'sha256':report['source_hash']}))
