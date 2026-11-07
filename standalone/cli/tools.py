"""n8n workflows, cleanup after a demonstration, the diagnostic report, the version."""
from __future__ import annotations

import contextlib
import io
import json
import platform
import re
import secrets
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from standalone.cli.common import (bad, bind_host, BUILD_INFO, database_target, DIAG_DIR, ENV_FILE, env_values,
                                   local_url, LOG_DIR, LOG_FILE, N8N_ORIGINAL_URL, N8N_OUT, N8N_SOURCE,
                                   N8N_WORKFLOWS, odbc_drivers, ok, parse_mssql_url, port_of, public_url,
                                   require_env, ROOT, say, schema_of, Stop, warn)
from standalone.cli.service import _http_status, _running, cmd_stop
from standalone.cli.check import _db_error, _prepare_app_environment, cmd_check


# --------------------------------------------------------------------------- n8n
def cmd_n8n(args) -> int:
    values = require_env()
    api_url = (args.api_url or public_url(values)).rstrip("/")
    prefix = args.prefix
    N8N_OUT.mkdir(exist_ok=True)
    say(f"Сценарии n8n с адресом API {api_url}:")
    for file_name in N8N_WORKFLOWS:
        source = N8N_SOURCE / file_name
        workflow = json.loads(source.read_text(encoding="utf-8").replace(N8N_ORIGINAL_URL, api_url))
        workflow["name"] = prefix + workflow.get("name", file_name)
        # An id lets administrators import the file with "n8n import:workflow" too (the editor's
        # "Import from File" ignores it); random, so it cannot replace another workflow.
        workflow["id"] = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
                                 for _ in range(16))
        workflow["versionId"] = str(uuid.uuid4())
        workflow["active"] = False
        for node in workflow.get("nodes", []):
            # New webhook ids: a shared n8n may already have the original workflows imported.
            if "webhookId" in node:
                node["webhookId"] = str(uuid.uuid4())
        target = N8N_OUT / file_name
        target.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        ok(f"{target.relative_to(ROOT)} — «{workflow['name']}»")
    say("\nВ n8n: Workflows → Import from File → файлы из папки n8n-demo.")
    if values.get("MAI_API_KEY"):
        say("API защищён ключом. В n8n: Credentials → New → Header Auth: Name = X-API-Key, Value = ключ")
        say("(mai.cmd link покажет его в ссылке после key=), затем выберите этот credential во всех HTTP-узлах сценария.")
    say("Проверка связи: в n8n добавьте HTTP Request на " + api_url + "/health/live и нажмите Execute step.")
    return 0


# --------------------------------------------------------------------------- cleanup
def cmd_cleanup(args) -> int:
    values = require_env()
    say("Будут удалены все таблицы демо-сборки в базе SQL Server (только таблицы этого приложения),")
    say("загруженные документы (data/documents), содержимое inbox и журналы. Настройки (.env) останутся.")
    if not args.yes:
        answer = input("Чтобы продолжить, напишите УДАЛИТЬ: ").strip()
        if answer != "УДАЛИТЬ":
            say("Отменено.")
            return 1
    pid, answering = _running(values)
    if pid or answering:
        cmd_stop(args)
    _prepare_app_environment()
    from sqlalchemy import text

    from app import db as app_db

    try:
        app_db.metadata.drop_all(app_db.engine())
        ok("таблицы удалены")
    except Exception as exc:  # noqa: BLE001
        bad(f"таблицы удалить не удалось: {_db_error(exc)}")
        return 1
    schema = schema_of(values)
    if schema and args.drop_schema:
        try:
            with app_db.engine().begin() as conn:
                conn.execute(text(f"DROP SCHEMA IF EXISTS [{schema}]"))
            ok(f"схема {schema} удалена")
        except Exception as exc:  # noqa: BLE001
            warn(f"схему {schema} удалить не удалось (в ней есть чужие объекты?): {_db_error(exc)}")
    for folder in (ROOT / "data" / "documents", ROOT / "inbox", LOG_DIR, N8N_OUT):
        if not folder.exists():
            continue
        for item in folder.iterdir():
            if item.name == ".gitkeep":
                continue
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
    ok("файлы удалены")
    say("\nГотово. Чтобы удалить сборку полностью — остановите её (mai.cmd stop) и удалите папку целиком.")
    return 0


# --------------------------------------------------------------------------- diag
SECRET_NAMES = re.compile(r"(PASSWORD|KEY|TOKEN|SECRET)", re.IGNORECASE)


def _masker(values: dict[str, str]):
    """A function hiding passwords, keys and tokens in a text (settings, check output, logs)."""
    secrets_ = {value for name, value in values.items() if SECRET_NAMES.search(name) and len(value) >= 4}
    for url_name in ("MAI_DATABASE_URL",):
        password = parse_mssql_url(values.get(url_name, "")).get("password")
        if password and len(password) >= 4:
            secrets_ |= {password, quote(password, safe="")}
    patterns = [(re.compile(r"([?&](?:key|api_key|token)=)[^&\s\"']+", re.IGNORECASE), r"\1***"),
                (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
                (re.compile(r"(://[^:/@\s]+:)[^@\s]+@"), r"\1***@")]

    def mask(text: str) -> str:
        for secret in sorted(secrets_, key=len, reverse=True):
            text = text.replace(secret, "***")
        for pattern, replacement in patterns:
            text = pattern.sub(replacement, text)
        return text
    return mask


def _log_tail(path: Path, lines: int) -> str:
    try:
        data = path.read_bytes()[-400_000:].decode("utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return "(нет файла)"
    return "\n".join(data) or "(пусто)"


def _folder_size(path: Path) -> str:
    try:
        total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return "?"
    return f"{total / 1048576:.1f} МБ"


def cmd_diag(args) -> int:
    """One text file for a bug report: everything except passwords, keys and the data itself."""
    values = env_values() if ENV_FILE.is_file() else {}
    mask = _masker(values)
    sections: list[tuple[str, str]] = []

    def section(title: str, produce) -> None:
        try:
            body = produce()
        except Exception as exc:  # noqa: BLE001 — a broken part must not stop the report
            body = f"(не удалось: {type(exc).__name__}: {exc})"
        sections.append((title, mask(str(body))))

    def build() -> str:
        info = json.loads(BUILD_INFO.read_text(encoding="utf-8")) if BUILD_INFO.is_file() else {}
        return "\n".join([f"сборка: {info.get('version', 'из исходников')} ({info.get('git_branch', '?')}, "
                          f"{info.get('git_commit', '?')[:12]})", f"собрана: {info.get('built_at', '?')}",
                          f"Python: {sys.version.split()[0]} ({sys.executable})",
                          f"система: {platform.platform()} ({platform.machine()})", f"папка: {ROOT}",
                          f"время отчёта: {datetime.now().astimezone().isoformat(timespec='seconds')}"])

    def settings_text() -> str:
        if not values:
            return "(.env нет — mai.cmd setup не выполнялся)"
        lines = []
        for name, value in values.items():
            shown = ("***" if value else "(пусто)") if SECRET_NAMES.search(name) else (value or "(пусто)")
            lines.append(f"{name}={shown}")
        return "\n".join(lines)

    def check_text() -> str:
        if args.no_check:
            return "(пропущено: --no-check)"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            try:
                code = cmd_check(args)
            except Stop as exc:
                say(str(exc))
                code = 1
        return buffer.getvalue() + f"\n(код завершения {code})"

    def state_text() -> str:
        lines = []
        if values:
            pid, answering = _running(values)
            lines.append(f"API: {'работает' if answering else 'не отвечает'}" + (f", процесс {pid}" if pid else ""))
            lines.append(f"адрес API: {local_url(values)}  (слушает {bind_host(values)}:{port_of(values)})")
            info = parse_mssql_url(values.get("MAI_DATABASE_URL", ""))
            lines.append(f"SQL Server: {database_target(values)}, вход {'Windows' if info.get('windows') else 'SQL'}, "
                         f"драйвер {info.get('driver') or '—'}; "
                         f"ODBC-драйверы на компьютере: {', '.join(odbc_drivers()) or 'нет'}")
            status, body = _http_status(f"{local_url(values)}/health/ready", timeout=5)
            lines.append(f"/health/ready: {status} {body[:400]}")
        usage = shutil.disk_usage(ROOT)
        lines.append(f"свободно на диске: {usage.free / 1073741824:.1f} ГБ из {usage.total / 1073741824:.1f} ГБ")
        for folder in (ROOT / "data" / "documents", ROOT / "inbox", LOG_DIR):
            lines.append(f"папка {folder.relative_to(ROOT)}: {_folder_size(folder)}")
        return "\n".join(lines)

    def documents_text() -> str:
        if not values or args.no_check:
            return "(пропущено)"
        _prepare_app_environment()
        from app import db as app_db

        counts = app_db.document_status_counts()
        lines = [f"по статусам: {counts or 'документов нет'}"]
        problems = [row for row in app_db.list_documents(limit=200) if row.get("status") in {"failed", "not_recognized"}]
        for index, row in enumerate(problems[:15], start=1):
            # File names may be confidential: only the extension and the size are reported.
            suffix = Path(row.get("filename") or "").suffix.lower() or "?"
            lines.append(f"  {index}. {row.get('status')} | документ{suffix}, {row.get('size_bytes')} байт | "
                         f"{row.get('created_at')} | {str(row.get('status_message') or '')[:300]}")
        return "\n".join(lines)

    say("Собираю отчёт (проверка может занять до минуты)…")
    section("Сборка и система", build)
    section("Настройки .env (пароли и ключи скрыты)", settings_text)
    section("Проверка (mai.cmd check)", check_text)
    section("Состояние", state_text)
    section("Документы с ошибками (имена файлов скрыты)", documents_text)
    section(f"Журнал приложения logs/api.log — последние {args.lines} строк", lambda: _log_tail(LOG_FILE, args.lines))
    DIAG_DIR.mkdir(exist_ok=True)
    target = DIAG_DIR / f"mai-diag-{datetime.now():%Y%m%d-%H%M%S}.txt"
    text_ = "\n\n".join(f"===== {title} =====\n{body}" for title, body in sections)
    target.write_text("Отчёт демо-сборки MAI Corrosion Control (mai.cmd diag)\n\n" + text_ + "\n", encoding="utf-8")
    say(f"\nОтчёт: {target}")
    say("Пароли, ключи и ключ в ссылках скрыты, имена файлов документов — тоже. В журнале могут быть номера")
    say("скважин: просмотрите файл перед отправкой и при необходимости замените их.")
    return 0


def cmd_version(args) -> int:
    if BUILD_INFO.is_file():
        info = json.loads(BUILD_INFO.read_text(encoding="utf-8"))
        for key in ("version", "git_commit", "git_branch", "built_at", "python", "platform"):
            if key in info:
                say(f"{key}: {info[key]}")
    else:
        say("Сборка из исходников (BUILD_INFO.json нет).")
    say(f"запущено на Python {sys.version.split()[0]}, папка: {ROOT}")
    return 0
