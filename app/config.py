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
    app_name: str = "MAI Corrosion Control API"
    app_version: str = "0.3.0"


settings = Settings()
try:
    ZoneInfo(settings.source_timezone)
except ZoneInfoNotFoundError as exc:
    raise ValueError("MAI_SOURCE_TIMEZONE must be a valid IANA timezone name") from exc
