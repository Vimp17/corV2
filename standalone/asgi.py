"""Entry point of the standalone build (no Docker): the regular application with three additions.

1. Archive data. With MAI_STANDALONE_DATA_DATE (the date of the latest data in the loaded files)
   the freshness, sample and act windows are widened at every start by the days passed since
   then, so a past export is not shown as «устарела», «нет пробы», «нет акта».
2. A dashboard link with the API key. With MAI_API_KEY set, opening /dashboard?key=<key> keeps the
   key in a cookie of this site and returns to /dashboard; a request with the cookie and without
   an X-API-Key header is let through as if it had the header. n8n and scripts send the header.
3. SQL Server. The tables are in Microsoft SQL Server (MAI_DATABASE_URL, a mssql+pyodbc or
   mssql+pymssql address, written by "mai.cmd setup"); without it the start stops with a hint.
Data sources are pulled only from the addresses set in .env (MAI_SOURCE_<ID>_URL); without them
data are loaded by hand (dashboard, inbox folder) or pushed to the API.

The application itself (app/) is not changed. Run: python -m uvicorn standalone.asgi:app
(standalone/manage.py does it).
"""
from __future__ import annotations

import os
import secrets
from datetime import date, datetime
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from standalone import envfile

ROOT = Path(__file__).resolve().parents[1]
KEY_COOKIE = "mai_api_key"
KEY_COOKIE_SECONDS = 30 * 86400
# Settings limits of app/config.py: windows wider than these are cut to them.
MAX_TELEMETRY_AGE_HOURS = 8760
MAX_WINDOW_DAYS = 3650


def _data_date(value: str) -> date | None:
    value = (value or "").strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    return None


def archive_windows(data_date: date | None, today: date | None = None) -> dict[str, str]:
    """Freshness, sample and act windows widened by the days since the latest data."""
    if data_date is None:
        return {}
    lag = max(0, ((today or date.today()) - data_date).days)
    return {
        # A week of slack: the demo may run a few days after the data were exported.
        "MAI_MAX_TELEMETRY_AGE_HOURS": str(min(MAX_TELEMETRY_AGE_HOURS, (lag + 7) * 24)),
        "MAI_RATING_SAMPLING_DAYS": str(min(MAX_WINDOW_DAYS, lag + 30)),
        "MAI_RATING_ACT_DAYS": str(min(MAX_WINDOW_DAYS, lag + 365)),
    }


def prepare_environment() -> dict:
    """Everything the standalone build sets before the application reads its settings."""
    env_path = ROOT / ".env"
    envfile.normalize(env_path)
    file_values = envfile.read(env_path)
    when = _data_date(os.environ.get("MAI_STANDALONE_DATA_DATE") or file_values.get("MAI_STANDALONE_DATA_DATE", ""))
    windows = archive_windows(when)
    os.environ.update(windows)
    url = os.environ.get("MAI_DATABASE_URL") or file_values.get("MAI_DATABASE_URL", "")
    if not url.startswith("mssql"):
        raise SystemExit("MAI_DATABASE_URL is not a SQL Server address (mssql+pyodbc:// or mssql+pymssql://): "
                         "run mai.cmd setup")
    return {"windows": windows}


PREPARED = prepare_environment()

from app.config import settings  # noqa: E402  (after the environment is prepared)
from app.main import app as core_app  # noqa: E402


async def _send_simple(send, status: int, headers: list[tuple[bytes, bytes]], body: bytes = b"") -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store"), *headers]})
    await send({"type": "http.response.body", "body": body})


def _cookie_key(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() != b"cookie":
            continue
        try:
            jar = SimpleCookie(value.decode("latin-1"))
        except CookieError:
            continue
        if KEY_COOKIE in jar:
            return unquote(jar[KEY_COOKIE].value)
    return None


class StandaloneApp:
    """The link with the key for the dashboard, and / leading to the dashboard."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope.get("path", ""), scope.get("method", "GET")
        query = scope.get("query_string", b"")
        if path == "/" and method in {"GET", "HEAD"}:
            location = b"/dashboard" + (b"?" + query if query else b"")
            return await _send_simple(send, 307, [(b"location", location)])
        key = settings.api_key
        if not key:
            return await self.app(scope, receive, send)
        if path in {"/dashboard", "/dashboard/"} and method in {"GET", "HEAD"} and query:
            given = (parse_qs(query.decode("latin-1")).get("key") or [None])[0]
            if given is not None:
                if not secrets.compare_digest(given.encode(), key.encode()):
                    body = ("<!doctype html><meta charset=utf-8><title>Неверный ключ</title>"
                            "<p>Ключ в ссылке не подходит. Попросите актуальную ссылку "
                            "(<code>mai.cmd link</code> на сервере).</p>").encode()
                    return await _send_simple(send, 403, [(b"content-type", b"text/html; charset=utf-8")], body)
                cookie = (f"{KEY_COOKIE}={quote(key, safe='')}; Path=/; Max-Age={KEY_COOKIE_SECONDS}; "
                          "HttpOnly; SameSite=Lax").encode()
                return await _send_simple(send, 303, [(b"location", b"/dashboard"), (b"set-cookie", cookie)])
        headers = scope.get("headers") or []
        if not any(name.lower() == b"x-api-key" for name, _ in headers):
            stored = _cookie_key(headers)
            if stored is not None:
                scope = {**scope, "headers": [*headers, (b"x-api-key", stored.encode("latin-1", errors="ignore"))]}
        return await self.app(scope, receive, send)


app = StandaloneApp(core_app)
