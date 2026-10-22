"""Entry point of the standalone demo build (no Docker): the regular application with three additions.

1. Nothing goes to data sources or Промайн. Before the application reads its settings, the pull
   addresses of every source profile (MAI_SOURCE_<ID>_URL / _TOKEN) are cleared and
   MAI_PROMINE_TRANSPORT is set to "none", whatever .env says. Data are loaded by hand: the
   dashboard («Загрузка данных», «Документы») or the inbox folder. The only outgoing calls left
   are those to the language model (LiteLLM).
2. Archive data. With MAI_STANDALONE_DATA_DATE (the date of the latest data in the loaded files)
   the freshness, sample and act windows are widened at every start by the days passed since
   then, so a past export is not shown as «устарела», «нет пробы», «нет акта».
3. A dashboard link with the API key. With MAI_API_KEY set, opening /dashboard?key=<key> keeps the
   key in a cookie of this site and returns to /dashboard; a request with the cookie and without
   an X-API-Key header is let through as if it had the header. n8n and scripts send the header.

The application itself (app/) is not changed. Run: python -m uvicorn standalone.asgi:app
(standalone/manage.py does it).
"""
from __future__ import annotations

import json
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


def _env_name(source_id: str) -> str:
    """MAI_SOURCE_<ID>_… name of a source, by the rule of app.core.source_connectors.source_env."""
    safe = "".join(char if char.isalnum() else "_" for char in source_id.upper())
    return "_".join(part for part in safe.split("_") if part)


def _profile_ids(file_values: dict[str, str]) -> set[str]:
    paths = {ROOT / "config" / "source_profiles.json"}
    configured = os.environ.get("MAI_SOURCE_PROFILES_PATH") or file_values.get("MAI_SOURCE_PROFILES_PATH")
    if configured:
        path = Path(configured).expanduser()
        paths.add(path if path.is_absolute() else (ROOT / path))
    ids: set[str] = set()
    for path in paths:
        try:
            profiles = json.loads(path.read_text(encoding="utf-8")).get("profiles") or {}
        except (OSError, ValueError):
            continue
        for profile_id, profile in profiles.items():
            if isinstance(profile, dict):
                ids.add(str(profile.get("source_id", profile_id)))
    return ids


def disable_outbound(file_values: dict[str, str]) -> list[str]:
    """Clear the source pull addresses and switch Промайн delivery off for this process."""
    names = {f"MAI_SOURCE_{_env_name(source_id)}_{suffix}"
             for source_id in _profile_ids(file_values) for suffix in ("URL", "TOKEN")}
    names |= {name for name in [*os.environ, *file_values]
              if name.startswith("MAI_SOURCE_") and name.endswith(("_URL", "_TOKEN"))}
    for name in sorted(names):
        os.environ[name] = ""
    os.environ["MAI_PROMINE_TRANSPORT"] = "none"
    return sorted(names)


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
    cleared = disable_outbound(file_values)
    when = _data_date(os.environ.get("MAI_STANDALONE_DATA_DATE") or file_values.get("MAI_STANDALONE_DATA_DATE", ""))
    windows = archive_windows(when)
    os.environ.update(windows)
    return {"cleared": cleared, "windows": windows}


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
