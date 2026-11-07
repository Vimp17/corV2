"""Paths, output and settings shared by the commands of mai.cmd."""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from standalone import envfile


ROOT = Path(__file__).resolve().parents[2]

ENV_FILE = ROOT / ".env"
TEMPLATE = ROOT / "standalone" / "env.template"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "api.log"
RUN_DIR = ROOT / "run"
PID_FILE = RUN_DIR / "api.pid"
DATA_DIRS = (ROOT / "data" / "documents", ROOT / "inbox", LOG_DIR, RUN_DIR)
N8N_SOURCE = ROOT / "n8n"
N8N_OUT = ROOT / "n8n-demo"
N8N_WORKFLOWS = ("scheduled_risk_monitor.json", "well_analysis_operator.json")
N8N_ORIGINAL_URL = "http://mai-api:8000"
BUILD_INFO = ROOT / "BUILD_INFO.json"
DIAG_DIR = ROOT / "diag"
ENTRY = "standalone.asgi:app"
WINDOWS = os.name == "nt"
LOG_ROTATE_BYTES = 20 * 1024 * 1024
START_TIMEOUT_SECONDS = 180
DEFAULTS = {"port": "8000", "timezone": "Europe/Moscow", "mssql_port": "1433"}
# Settings of earlier builds (built-in PostgreSQL, SQLite; the old name of the schema): removed by setup.
OBSOLETE_SETTINGS = {"MAI_STANDALONE_DB", "MAI_STANDALONE_PG_PORT", "MAI_STANDALONE_PG_PASSWORD",
                     "MAI_STANDALONE_PG_BIN", "MAI_STANDALONE_MSSQL_SCHEMA"}
# Schema names of SQL Server.
MSSQL_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


# --------------------------------------------------------------------------- output
def say(text: str = "") -> None:
    print(text, flush=True)


def ok(text: str) -> None:
    say(f"  [OK]  {text}")


def warn(text: str) -> None:
    say(f"  [!]   {text}")


def bad(text: str) -> None:
    say(f"  [X]   {text}")


def note(text: str) -> None:
    say(f"        {text}")


class Stop(Exception):
    """A user-facing error: printed without a traceback, exit code 1."""


# --------------------------------------------------------------------------- settings
def env_values() -> dict[str, str]:
    envfile.normalize(ENV_FILE)
    return envfile.read(ENV_FILE)


def require_env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        raise Stop("Настроек ещё нет: сначала выполните  mai.cmd setup")
    values = env_values()
    if not values.get("MAI_DATABASE_URL"):
        raise Stop("В .env не задана база данных (MAI_DATABASE_URL): выполните  mai.cmd setup")
    return values


def bind_host(values: dict[str, str]) -> str:
    return values.get("MAI_STANDALONE_HOST") or "127.0.0.1"


def port_of(values: dict[str, str]) -> int:
    return int(values.get("MAI_STANDALONE_PORT") or DEFAULTS["port"])


def local_url(values: dict[str, str]) -> str:
    host = bind_host(values)
    host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    return f"http://{host}:{port_of(values)}"


def public_url(values: dict[str, str]) -> str:
    return (values.get("MAI_STANDALONE_PUBLIC_URL") or local_url(values)).rstrip("/")


def dashboard_link(values: dict[str, str]) -> str:
    key = values.get("MAI_API_KEY", "")
    base = f"{public_url(values)}/dashboard"
    return f"{base}?key={quote(key, safe='')}" if key else base


PYMSSQL = "pymssql"


def build_mssql_url(host: str, port: str, name: str, user: str, password: str, driver: str, trust: bool) -> str:
    """mssql+pyodbc URL with an ODBC driver, or mssql+pymssql (driver "pymssql": no ODBC driver needed);
    no user = Windows authentication of the account the build runs under."""
    from sqlalchemy.engine import URL

    if driver == PYMSSQL:
        url = URL.create("mssql+pymssql", username=user or None, password=password or None, host=host,
                         port=int(port) if port else None, database=name)
        return url.render_as_string(hide_password=False)
    query = {"driver": driver, "TrustServerCertificate": "yes" if trust else "no"}
    if not user:
        query["Trusted_Connection"] = "yes"
    url = URL.create("mssql+pyodbc", username=user or None, password=password or None, host=host,
                     port=int(port) if port else None, database=name, query=query)
    return url.render_as_string(hide_password=False)


def parse_mssql_url(url: str) -> dict:
    if not url.startswith("mssql"):
        return {}
    from sqlalchemy.engine import make_url

    parts = make_url(url)
    query = {key.lower(): value for key, value in parts.query.items()}
    driver = PYMSSQL if parts.drivername.endswith(PYMSSQL) else query.get("driver", "")
    return {"host": parts.host or "", "port": str(parts.port or ""), "name": parts.database or "",
            "user": parts.username or "", "password": parts.password or "", "driver": driver,
            "trust": str(query.get("trustservercertificate", "yes")).lower() == "yes",
            "windows": not parts.username}


def schema_of(values: dict[str, str]) -> str:
    """The schema of the tables (MAI_STANDALONE_MSSQL_SCHEMA in .env files of earlier builds)."""
    return values.get("MAI_DATABASE_SCHEMA") or values.get("MAI_STANDALONE_MSSQL_SCHEMA", "")


def database_target(values: dict[str, str]) -> str:
    """server[,port]/database, schema — for messages (no login, no password)."""
    info = parse_mssql_url(values.get("MAI_DATABASE_URL", ""))
    if not info:
        return "не настроен (mai.cmd setup)"
    schema = schema_of(values) or "по умолчанию для входа"
    return f"{info['host']}" + (f",{info['port']}" if info["port"] else "") + f"/{info['name']}, схема {schema}"


def odbc_drivers() -> list[str]:
    """ODBC drivers of SQL Server on this computer, newest first (empty without pyodbc)."""
    try:
        import pyodbc
    except Exception:  # noqa: BLE001 — not in this build, or the ODBC driver manager is missing
        return []
    found = [name for name in pyodbc.drivers() if name.startswith("ODBC Driver") and "SQL Server" in name]
    return sorted(found, key=lambda name: int(re.sub(r"\D", "", name) or 0), reverse=True)


def normalize_llm_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url or url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    if urlsplit(url).path in {"", "/"}:
        return url + "/v1/chat/completions"
    return url + "/chat/completions"


def parse_date(value: str) -> date | None:
    value = (value or "").strip()
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    return None
