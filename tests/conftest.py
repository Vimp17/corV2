"""Test settings. Integration tests need SQL Server: set MAI_TEST_DATABASE_URL to a database they
may fill and clear (MAI_TEST_DATABASE_SCHEMA: a schema other than the default one of the login), e.g.

    MAI_TEST_DATABASE_URL="mssql+pymssql://sa:...@localhost:1433/mai_test" pytest
    MAI_TEST_DATABASE_URL="mssql+pyodbc://sa:...@localhost:1433/mai_test?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes" pytest

Without it only the tests that need no database run. LiteLLM is replaced by tests.fake_litellm.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Settings are read once at import, so the environment is fixed before the app is imported.
# The module runs twice (as pytest's conftest and as tests.conftest imported by the tests): both
# runs must use the same folder, whichever of them the application settings were read in.
_TMP = Path(os.environ.setdefault("MAI_TESTS_TMP", tempfile.mkdtemp(prefix="mai-tests-")))
os.environ["MAI_DATABASE_URL"] = os.environ.get("MAI_TEST_DATABASE_URL", "")
os.environ["MAI_DATABASE_SCHEMA"] = os.environ.get("MAI_TEST_DATABASE_SCHEMA", "")
os.environ.update({
    "MAI_API_KEY": "",
    "MAI_LLM_API_URL": "http://fake-litellm.local/v1/chat/completions",
    "MAI_LLM_MODEL": "fake-llm",
    "MAI_OCR_MODEL": "fake-ocr",
    "MAI_DOCS_WORKER": "false",
    "MAI_DOCS_STORAGE_DIR": str(_TMP / "documents"),
    "MAI_DOCS_INBOX_DIR": str(_TMP / "inbox"),
    "MAI_DOCS_RETRY_MINUTES": "1",
    # The demonstration math model of the repository, whatever a developer's .env names.
    "MAI_MATH_MODEL_PATH": str(ROOT / "config" / "math_model.json"),
})
(_TMP / "inbox").mkdir(parents=True, exist_ok=True)

TEST_TMP = _TMP


# --------------------------------------------------------------------------- shared by the document, Excel and agent tests
TODAY = date.today()


# --------------------------------------------------------------------------- with the database
needs_db = pytest.mark.skipif(not os.environ.get("MAI_TEST_DATABASE_URL"),
                              reason="set MAI_TEST_DATABASE_URL to run the SQL Server tests")


@pytest.fixture(scope="session")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_sample_data.py"), "--end-date", TODAY.isoformat(),
                    "--out", str(out)], check=True, capture_output=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_demo_documents.py"), "--end-date",
                    TODAY.isoformat(), "--out", str(out / "documents")], check=True, capture_output=True)
    return out


@pytest.fixture()
def fake(monkeypatch):
    from app.core import llm
    from scripts.build_demo_documents import scan_lines
    from tests.fake_litellm import FakeLiteLLM

    stub = FakeLiteLLM("\n".join(scan_lines(TODAY)))
    monkeypatch.setattr(llm, "chat", stub.chat)
    return stub


@pytest.fixture()
def client(generated, fake):
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app

    with TestClient(app) as test_client:
        with db.engine().begin() as conn:
            for table in reversed(db.metadata.sorted_tables):
                if table.name != "monitor_jobs":
                    conn.execute(table.delete())
        for profile, name in (("dars", "sample_dars.xlsx"), ("khimik", "sample_khimik.xlsx"), ("era", "sample_era.xlsx")):
            with open(generated / name, "rb") as handle:
                response = test_client.post("/api/v1/ingestion/excel", data={"profile_id": profile},
                                            files={"file": (name, handle)})
            assert response.status_code == 201, response.text
        yield test_client


def _upload(client, path, wait=True):
    with open(path, "rb") as handle:
        response = client.post(f"/api/v1/documents?wait={'true' if wait else 'false'}",
                               files=[("files", (path.name, handle))])
    assert response.status_code == 201, response.text
    return response.json()["documents"][0]


def _well(client, well_id):
    response = client.get(f"/api/v1/rating/wells/{well_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _gap_keys(well):
    return {item["key"] for item in well["data_gaps"]}
