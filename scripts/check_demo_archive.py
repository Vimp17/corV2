"""Verify the newly built archive without running the Windows executable on Linux."""
import hashlib
import json
import zipfile
from pathlib import Path

folder = Path('standalone/dist')
info = json.loads((folder / 'last-build.json').read_text())
archive = folder / f"mai-demo-win64-{info['version']}.zip"
assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive.with_suffix('.zip.sha256').read_text().split()[0]
assert not info['dll_problems'], info['dll_problems']
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
    names = z.namelist()
    root = names[0].split('/')[0] + '/'
    required = ['app/core/co2_corrosion.py', 'app/core/co2_import.py', 'app/api/routes/co2.py',
                'app/db/co2.py', 'examples/co2/v4-example.json', 'runtime/python.exe', 'BUILD_INFO.json']
    assert all(root + name in names for name in required)
    assert not any(n.endswith('/.env') or 'calculation_rows.json' in n or 'co2-model-review' in n for n in names)
    manifest = json.loads(z.read(root + 'BUILD_INFO.json'))
    assert manifest['git_commit'] == info['git_commit']
    assert not manifest['dll_problems']
print(f"Verified {archive.name}: CRC, SHA-256, manifest, CO2 modules, no full dataset or .env")
