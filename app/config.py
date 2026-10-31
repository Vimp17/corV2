from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_simple_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if name:
            os.environ.setdefault(name, value)


_load_simple_env_file(PROJECT_ROOT / ".env")


def _int_env(name: str, default: str, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _positive_float(name: str, default: str, maximum: float) -> float:
    value = float(os.getenv(name, default))
    if not 0 < value <= maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum}")
    return value


def _fraction_env(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _project_path(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default)).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class Settings:
    # Microsoft SQL Server: mssql+pyodbc://…?driver=ODBC+Driver+18+for+SQL+Server or mssql+pymssql://…
    # (app/db.py); the schema of the tables, empty = the default schema of the login (dbo). The schema
    # was MAI_STANDALONE_MSSQL_SCHEMA in earlier versions; .env files written then still work.
    database_url: str = os.getenv("MAI_DATABASE_URL", "").strip()
    database_schema: str = (os.getenv("MAI_DATABASE_SCHEMA") or os.getenv("MAI_STANDALONE_MSSQL_SCHEMA", "")).strip()
    source_profiles_path: Path = _project_path("MAI_SOURCE_PROFILES_PATH", "./config/source_profiles.json")
    max_forward_fill_days: int = _int_env("MAI_MAX_FORWARD_FILL_DAYS", "7", 0, 365)
    max_telemetry_age_hours: float = _positive_float("MAI_MAX_TELEMETRY_AGE_HOURS", "48", 8760)
    min_telemetry_records: int = _int_env("MAI_MIN_TELEMETRY_RECORDS", "8", 2, 10000)
    min_trend_observations: int = _int_env("MAI_MIN_TREND_OBSERVATIONS", "8", 2, 10000)
    trend_window_days: int = _int_env("MAI_TREND_WINDOW_DAYS", "28", 1, 3650)
    min_current_signals: int = _int_env("MAI_MIN_CURRENT_SIGNALS", "2", 1, 10)
    api_key: str = os.getenv("MAI_API_KEY", "").strip()
    llm_api_url: str = os.getenv("MAI_LLM_API_URL", "").strip()
    llm_api_key: str = os.getenv("MAI_LLM_API_KEY", "").strip()
    llm_model: str = os.getenv("MAI_LLM_MODEL", "").strip()
    llm_timeout_seconds: float = _positive_float("MAI_LLM_TIMEOUT_SECONDS", "45", 600)
    llm_max_retries: int = _int_env("MAI_LLM_MAX_RETRIES", "1", 0, 3)
    source_timezone: str = os.getenv("MAI_SOURCE_TIMEZONE", "Europe/Moscow").strip()
    max_excel_bytes: int = _int_env("MAI_MAX_EXCEL_BYTES", str(15 * 1024 * 1024), 1024, 100 * 1024 * 1024)
    max_excel_rows: int = _int_env("MAI_MAX_EXCEL_ROWS", "10000", 1, 100000)
    configured_well_ids: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv("MAI_WELL_IDS", "").split(",") if item.strip()
    )
    dashboard_url: str = os.getenv("MAI_DASHBOARD_URL", "http://localhost:8000/dashboard").strip()
    # Scheduled monitor: wells whose sources are refreshed concurrently, and retries for
    # transient source API failures (connection errors, timeouts, HTTP 429/502/503/504).
    source_refresh_workers: int = _int_env("MAI_SOURCE_REFRESH_WORKERS", "4", 1, 32)
    source_max_retries: int = _int_env("MAI_SOURCE_MAX_RETRIES", "2", 0, 5)
    # Readiness tolerance for clocks running ahead of the server; beyond it analysis is skipped.
    max_future_skew_hours: float = _positive_float("MAI_MAX_FUTURE_SKEW_HOURS", "24", 720)
    # Well rating (approved Excel layout): index = НТЦ score × 100, computed from source data
    # (DARS, ХИМИК, ERA, documents) by app.core.rating_builder.
    # Thresholds: index >= repair_from -> planned repair package + ERA repair inclusion;
    # ud_from..ud_to -> shock dosing (УД); between ud_to and repair_from -> engineer decision.
    rating_repair_from: int = _int_env("MAI_RATING_REPAIR_FROM", "80", 1, 100)
    rating_ud_from: int = _int_env("MAI_RATING_UD_FROM", "40", 0, 100)
    rating_ud_to: int = _int_env("MAI_RATING_UD_TO", "60", 0, 100)
    # Days from the date the current measure was first assigned to its deadline.
    rating_days_repair: int = _int_env("MAI_RATING_DAYS_REPAIR", "14", 1, 365)
    rating_days_engineer: int = _int_env("MAI_RATING_DAYS_ENGINEER", "30", 1, 365)
    rating_days_ud: int = _int_env("MAI_RATING_DAYS_UD", "45", 1, 365)
    # «Отбор проб» = a laboratory sample (profile with "lab_samples": true) within this many days.
    rating_sampling_days: int = _int_env("MAI_RATING_SAMPLING_DAYS", "30", 1, 3650)
    # Demount and base-inspection acts older than this are not counted.
    rating_act_days: int = _int_env("MAI_RATING_ACT_DAYS", "365", 1, 3650)
    # Protection (inhibitor, coated tubing): "adjust" counts the environment rule points with
    # the factor when protection is confirmed effective; "info" only shows the status.
    rating_protection_mode: str = os.getenv("MAI_RATING_PROTECTION_MODE", "adjust").strip().lower()
    rating_protection_factor: float = _fraction_env("MAI_RATING_PROTECTION_FACTOR", "0.5")
    rating_protection_min_inhibitor: float = _fraction_env("MAI_RATING_PROTECTION_MIN_INHIBITOR", "0.80")
    rating_protection_max_injection_dev: float = _positive_float(
        "MAI_RATING_PROTECTION_MAX_INJECTION_DEV", "15", 1000)
    rating_protection_max_rate: float = _positive_float("MAI_RATING_PROTECTION_MAX_RATE", "0.1", 100)
    # Uploads touching at most this many wells recompute their rating before responding;
    # larger uploads recompute in the background.
    rating_recompute_sync_limit: int = _int_env("MAI_RATING_RECOMPUTE_SYNC_LIMIT", "50", 0, 100000)
    # Documents (Excel, PDF, scans and photos): originals are kept under docs_storage_dir; files
    # dropped into docs_inbox_dir (empty = no inbox) are picked up every docs_inbox_scan_seconds.
    docs_storage_dir: Path = _project_path("MAI_DOCS_STORAGE_DIR", "./data/documents")
    docs_inbox_dir: str = os.getenv("MAI_DOCS_INBOX_DIR", "").strip()
    docs_inbox_scan_seconds: int = _int_env("MAI_DOCS_INBOX_SCAN_SECONDS", "300", 10, 86400)
    # Background worker that processes queued documents and scans the inbox (off = only the
    # POST /documents/process-queue call processes them, e.g. from n8n).
    docs_worker_enabled: bool = os.getenv("MAI_DOCS_WORKER", "true").strip().lower() not in {"0", "false", "no", "off"}
    # OCR / LLM unavailable: the document is retried this many times, this many minutes apart.
    docs_max_attempts: int = _int_env("MAI_DOCS_MAX_ATTEMPTS", "3", 1, 20)
    docs_retry_minutes: int = _int_env("MAI_DOCS_RETRY_MINUTES", "5", 1, 1440)
    docs_types_path: Path = _project_path("MAI_DOCS_TYPES_PATH", "./config/document_types.json")
    docs_max_bytes: int = _int_env("MAI_DOCS_MAX_BYTES", str(25 * 1024 * 1024), 1024, 200 * 1024 * 1024)
    docs_max_pages: int = _int_env("MAI_DOCS_MAX_PAGES", "30", 1, 500)
    # A PDF page with fewer characters in its text layer is treated as a scan and sent to OCR.
    docs_min_page_chars: int = _int_env("MAI_DOCS_MIN_PAGE_CHARS", "40", 0, 10000)
    # Characters of document text sent to the extraction model.
    docs_max_text_chars: int = _int_env("MAI_DOCS_MAX_TEXT_CHARS", "60000", 1000, 1000000)
    # Model that classifies documents and extracts fields (LiteLLM chat completions); empty =
    # MAI_LLM_MODEL. The OCR model reads page images through the same LiteLLM (vision messages).
    docs_llm_model: str = os.getenv("MAI_DOCS_LLM_MODEL", "").strip()
    ocr_api_url: str = os.getenv("MAI_OCR_API_URL", "").strip()
    ocr_api_key: str = os.getenv("MAI_OCR_API_KEY", "").strip()
    ocr_model: str = os.getenv("MAI_OCR_MODEL", "").strip()
    ocr_timeout_seconds: float = _positive_float("MAI_OCR_TIMEOUT_SECONDS", "120", 900)
    ocr_max_side_px: int = _int_env("MAI_OCR_MAX_SIDE_PX", "2000", 500, 6000)
    # Examples of response measures given to the AI agent when it drafts a well proposal.
    response_examples_path: Path = _project_path("MAI_RESPONSE_EXAMPLES_PATH", "./config/response_examples.json")
    app_name: str = "MAI Corrosion Control API"
    app_version: str = "0.8.0"

    @property
    def extraction_model(self) -> str:
        return self.docs_llm_model or self.llm_model

    @property
    def ocr_url(self) -> str:
        return self.ocr_api_url or self.llm_api_url

    @property
    def ocr_key(self) -> str:
        return self.ocr_api_key if self.ocr_api_url else (self.ocr_api_key or self.llm_api_key)


settings = Settings()
if settings.rating_protection_mode not in {"adjust", "info"}:
    raise ValueError("MAI_RATING_PROTECTION_MODE must be adjust or info")
if not settings.rating_ud_from < settings.rating_ud_to < settings.rating_repair_from:
    raise ValueError("Rating thresholds must satisfy MAI_RATING_UD_FROM < MAI_RATING_UD_TO < MAI_RATING_REPAIR_FROM")
try:
    ZoneInfo(settings.source_timezone)
except ZoneInfoNotFoundError as exc:
    raise ValueError("MAI_SOURCE_TIMEZONE must be a valid IANA timezone name") from exc
