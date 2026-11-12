"""Re-run the same verification used by the build itself."""
import importlib.util
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("builder", root / "standalone/build/build_windows.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
info = json.loads((builder.DIST / "last-build.json").read_text())
archive = builder.DIST / f"mai-demo-win64-{info['version']}.zip"
builder.validate_archive(archive, info)
print(f"Verified {archive.name}: CRC, SHA-256, manifest and contents")
