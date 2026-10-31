"""Test settings. Integration tests need PostgreSQL: set MAI_TEST_DATABASE_URL to an empty
database they may fill and truncate, e.g.

    MAI_TEST_DATABASE_URL="postgresql+psycopg://mai:mai@localhost:5432/mai_test" pytest

Without it only the tests that need no database run. LiteLLM is replaced by tests.fake_litellm.

The same tests run on SQL Server through the MSSQL mode of the standalone build
(standalone/mssql_compat.py): a mssql+pyodbc URL, and MAI_TEST_MSSQL_SCHEMA for a schema other
than the default one of the login, e.g.

    MAI_TEST_DATABASE_URL="mssql+pyodbc://sa:...@localhost:1433/mai_test?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes" pytest
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Settings are read once at import, so the environment is fixed before the app is imported.
# The module runs twice (as pytest's conftest and as tests.conftest imported by the tests): both
# runs must use the same folder, whichever of them the application settings were read in.
_TMP = Path(os.environ.setdefault("MAI_TESTS_TMP", tempfile.mkdtemp(prefix="mai-tests-")))
os.environ["MAI_DATABASE_URL"] = os.environ.get("MAI_TEST_DATABASE_URL", "postgresql+psycopg://invalid/none")
os.environ.update({
    "MAI_API_KEY": "",
    "MAI_LLM_API_URL": "http://fake-litellm.local/v1/chat/completions",
    "MAI_LLM_MODEL": "fake-llm",
    "MAI_OCR_MODEL": "fake-ocr",
    "MAI_DOCS_WORKER": "false",
    "MAI_DOCS_STORAGE_DIR": str(_TMP / "documents"),
    "MAI_DOCS_INBOX_DIR": str(_TMP / "inbox"),
    "MAI_DOCS_RETRY_MINUTES": "1",
})
(_TMP / "inbox").mkdir(parents=True, exist_ok=True)
if os.environ["MAI_DATABASE_URL"].startswith("mssql"):
    from standalone import mssql_compat

    mssql_compat.install(os.environ["MAI_DATABASE_URL"], os.environ.get("MAI_TEST_MSSQL_SCHEMA", ""))

TEST_TMP = _TMP
