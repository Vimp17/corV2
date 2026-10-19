"""Test settings. Integration tests need PostgreSQL: set MAI_TEST_DATABASE_URL to an empty
database they may fill and truncate, e.g.

    MAI_TEST_DATABASE_URL="postgresql+psycopg://mai:mai@localhost:5432/mai_test" pytest

Without it only the tests that need no database run. LiteLLM is replaced by tests.fake_litellm.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Settings are read once at import, so the environment is fixed before the app is imported.
_TMP = Path(tempfile.mkdtemp(prefix="mai-tests-"))
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

TEST_TMP = _TMP
