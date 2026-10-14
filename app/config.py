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


def _probability_threshold() -> float:
    value = float(os.getenv("MAI_ALERT_PROBABILITY_THRESHOLD", "0.7"))
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("MAI_ALERT_PROBABILITY_THRESHOLD must be between 0 and 1")
    return value


def _project_path(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default)).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "MAI_DATABASE_URL",
        "postgresql+psycopg://mai:mai_dev_only@localhost:5432/mai_corrosion",
    ).strip()
    cox_model_path: Path = _project_path("MAI_COX_MODEL_PATH", "./models/cox_model.json")
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
    alert_rule_classes: tuple[str, ...] = tuple(
        item.strip().upper() for item in os.getenv("MAI_ALERT_RULE_CLASSES", "HIGH,CRITICAL").split(",")
        if item.strip()
    )
    alert_probability_threshold: float = _probability_threshold()
    dashboard_url: str = os.getenv("MAI_DASHBOARD_URL", "http://localhost:8000/dashboard").strip()
    # Full analysis payloads older than this are pruned after each scheduled run; the latest
    # run for every well/horizon pair is always kept. 0 disables pruning.
    analysis_retention_days: int = _int_env("MAI_ANALYSIS_RETENTION_DAYS", "90", 0, 3650)
    # Scheduled monitor: wells whose sources are refreshed concurrently, and retries for
    # transient source API failures (connection errors, timeouts, HTTP 429/502/503/504).
    source_refresh_workers: int = _int_env("MAI_SOURCE_REFRESH_WORKERS", "4", 1, 32)
    source_max_retries: int = _int_env("MAI_SOURCE_MAX_RETRIES", "2", 0, 5)
    # Readiness tolerance for clocks running ahead of the server; beyond it analysis is skipped.
    max_future_skew_hours: float = _positive_float("MAI_MAX_FUTURE_SKEW_HOURS", "24", 720)
    # Well rating (approved Excel layout): index = combined НТЦ / math model score × 100.
    # The rating is computed from source data (DARS, ХИМИК, ERA) by app.core.rating_builder.
    # Thresholds: index >= repair_from -> planned repair package + ERA repair inclusion;
    # ud_from..ud_to -> shock dosing (УД); between ud_to and repair_from -> engineer decision.
    rating_combine: str = os.getenv("MAI_RATING_COMBINE", "max").strip().lower()
    rating_repair_from: int = _int_env("MAI_RATING_REPAIR_FROM", "80", 1, 100)
    rating_ud_from: int = _int_env("MAI_RATING_UD_FROM", "40", 0, 100)
    rating_ud_to: int = _int_env("MAI_RATING_UD_TO", "60", 0, 100)
    # Days from the date the current measure was first assigned to its deadline.
    rating_days_repair: int = _int_env("MAI_RATING_DAYS_REPAIR", "14", 1, 365)
    rating_days_engineer: int = _int_env("MAI_RATING_DAYS_ENGINEER", "30", 1, 365)
    rating_days_ud: int = _int_env("MAI_RATING_DAYS_UD", "45", 1, 365)
    # Forecast horizon whose model scores feed the rating (НТЦ / math columns).
    rating_horizon_days: int = _int_env("MAI_RATING_HORIZON_DAYS", "90", 1, 3650)
    # «Отбор проб» = a laboratory sample (profile with "lab_samples": true) within this many days.
    rating_sampling_days: int = _int_env("MAI_RATING_SAMPLING_DAYS", "30", 1, 3650)
    # Demount and base-inspection acts older than this are not counted.
    rating_act_days: int = _int_env("MAI_RATING_ACT_DAYS", "365", 1, 3650)
    # Uploads touching at most this many wells recompute their rating before responding;
    # larger uploads recompute in the background.
    rating_recompute_sync_limit: int = _int_env("MAI_RATING_RECOMPUTE_SYNC_LIMIT", "50", 0, 100000)
    # Промайн: proposals to change the ERA repair plan. Transport "smtp" mails the Промайн
    # mailbox, "api" posts JSON to the Промайн endpoint, "none" stores proposals unsent.
    promine_transport: str = os.getenv("MAI_PROMINE_TRANSPORT", "none").strip().lower()
    promine_email_to: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv("MAI_PROMINE_EMAIL_TO", "").split(",") if item.strip()
    )
    promine_api_url: str = os.getenv("MAI_PROMINE_API_URL", "").strip()
    promine_api_token: str = os.getenv("MAI_PROMINE_API_TOKEN", "").strip()
    promine_timeout_seconds: float = _positive_float("MAI_PROMINE_TIMEOUT_SECONDS", "20", 120)
    smtp_host: str = os.getenv("MAI_SMTP_HOST", "").strip()
    smtp_port: int = _int_env("MAI_SMTP_PORT", "587", 1, 65535)
    smtp_user: str = os.getenv("MAI_SMTP_USER", "").strip()
    smtp_password: str = os.getenv("MAI_SMTP_PASSWORD", "")
    smtp_security: str = os.getenv("MAI_SMTP_SECURITY", "starttls").strip().lower()
    smtp_from: str = os.getenv("MAI_SMTP_FROM", "").strip()
    app_name: str = "MAI Corrosion Control API"
    app_version: str = "0.5.0"


settings = Settings()
if settings.rating_combine not in {"max", "mean"}:
    raise ValueError("MAI_RATING_COMBINE must be max or mean")
if not settings.rating_ud_from < settings.rating_ud_to < settings.rating_repair_from:
    raise ValueError("Rating thresholds must satisfy MAI_RATING_UD_FROM < MAI_RATING_UD_TO < MAI_RATING_REPAIR_FROM")
if settings.promine_transport not in {"none", "smtp", "api"}:
    raise ValueError("MAI_PROMINE_TRANSPORT must be none, smtp or api")
if settings.smtp_security not in {"starttls", "ssl", "none"}:
    raise ValueError("MAI_SMTP_SECURITY must be starttls, ssl or none")
try:
    ZoneInfo(settings.source_timezone)
except ZoneInfoNotFoundError as exc:
    raise ValueError("MAI_SOURCE_TIMEZONE must be a valid IANA timezone name") from exc
