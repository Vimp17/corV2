"""mai.cmd check: packages, database (and its tables), language models, port."""
from __future__ import annotations

import os
import re
import sys
import time
import uuid

from standalone import envfile
from standalone.cli.common import (bad, bind_host, dashboard_link, DATA_DIRS, ENV_FILE, local_url, note,
                                   odbc_drivers, ok, parse_mssql_url, port_of, PYMSSQL, require_env, ROOT, say,
                                   schema_of, Stop, warn, WINDOWS)
from standalone.cli.service import _port_free, _running


# --------------------------------------------------------------------------- check
def _prepare_app_environment() -> dict:
    """Import the standalone entry point exactly as the API process does (settings, guards)."""
    import logging

    os.chdir(ROOT)
    from standalone import asgi

    # The application logs at INFO; in the console only problems are of interest.
    for name in ("alembic", "app", "mai_api"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return asgi.PREPARED


def cmd_check(args) -> int:
    failures = 0

    def fail(text: str) -> None:
        nonlocal failures
        failures += 1
        bad(text)

    say("Проверка демо-сборки\n")
    say("Система")
    ok(f"Python {sys.version.split()[0]} ({sys.executable})")
    if WINDOWS and len(str(ROOT)) > 60:
        warn(f"Длинный путь к папке ({len(str(ROOT))} символов): лучше короткий, например C:\\mai-demo")
    if not str(ROOT).isascii():
        warn("В пути к папке есть не латинские символы — лучше путь латиницей, например C:\\mai-demo")
    for folder in DATA_DIRS:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / f".write-test-{uuid.uuid4().hex}"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            fail(f"Нет записи в папку {folder.relative_to(ROOT)}: {exc}")
    try:
        fixed = envfile.normalize(ENV_FILE)
        if fixed:
            warn(f".env исправлен: {fixed}")
        values = require_env()
        ok("Настройки .env прочитаны")
    except Stop as exc:
        fail(str(exc))
        return 1

    say("\nПакеты")
    modules = ("sqlalchemy", "fastapi", "uvicorn",
               "openpyxl", "xlrd", "pypdfium2", "PIL", "requests", "tzdata", "pyodbc", "pymssql")
    missing = []
    for module in modules:
        try:
            __import__(module)
        except Exception as exc:  # noqa: BLE001 — a DLL error is an ImportError or OSError
            missing.append(f"{module}: {type(exc).__name__}: {str(exc)[:160]}")
    if missing:
        for item in missing:
            fail(item)
        if WINDOWS and any("DLL" in item for item in missing):
            note("Не хватает системной библиотеки: нужен «Microsoft Visual C++ Redistributable 2015–2022 (x64)»")
            note("(ставит администратор), см. README-DEMO.md, «Неполадки».")
        return 1
    ok("все пакеты загружаются")

    say("\nБаза данных")
    problem = _check_mssql(values)
    if problem:
        fail(problem)
        return 1

    prepared = _prepare_app_environment()
    from app import db as app_db
    from app.config import settings

    try:
        changes = app_db.init_db()
    except Exception as exc:  # noqa: BLE001
        fail(f"таблицы приложения: {str(exc).strip()[:500]}")
        return 1
    total = len(app_db.metadata.tables)
    ok(f"таблицы приложения на месте: {total} из {total}"
       + (f" ({app_db.describe_changes(changes)})" if any(changes.values()) else ""))

    say("\nЯзыковая модель")
    if not (settings.llm_api_url and settings.llm_model):
        warn("не настроена: предложения ИИ-агента, распознавание PDF и сканов не работают")
    else:
        failures += _check_llm(settings)

    say("\nСеть")
    host, port = bind_host(values), port_of(values)
    pid, answering = _running(values)
    if pid or answering:
        ok(f"API уже запущен и отвечает: {local_url(values)}/dashboard")
    elif _port_free(host, port):
        ok(f"порт {port} свободен")
    else:
        fail(f"порт {port} занят другой программой — выберите другой (mai.cmd setup)")
    if host not in {"127.0.0.1", "localhost"}:
        note("Доступ с других компьютеров и из n8n зависит от брандмауэра: после запуска откройте ссылку")
        note(f"с другого компьютера: {dashboard_link(values)}")
        if not values.get("MAI_API_KEY"):
            warn("ключ доступа не задан — данные откроются всем в сети, кто знает адрес")

    say("\nИсточники данных")
    try:
        from app.core.source_connectors import source_modes

        for source in source_modes():
            if source["mode"] == "adapter_missing":
                fail(f"{source['title']}: адаптер не установлен")
            elif source["mode"] in {"api", "adapter"}:
                warn(f"{source['title']}: подключение задано, связь ещё не проверена")
            else:
                ok(f"{source['title']}: загрузка файлов / push API")
        note("Проверка ответа без импорта: mai.cmd sources check --well <ID>")
    except Exception as exc:  # noqa: BLE001
        fail(f"профили источников не читаются: {str(exc)[:200]}")
    if prepared["windows"]:
        windows = prepared["windows"]
        ok(f"архивные данные на {values.get('MAI_STANDALONE_DATA_DATE')}: телеметрия свежая "
           f"{int(windows['MAI_MAX_TELEMETRY_AGE_HOURS']) // 24} сут., пробы {windows['MAI_RATING_SAMPLING_DAYS']} сут., "
           f"акты {windows['MAI_RATING_ACT_DAYS']} сут.")

    say("")
    if failures:
        say(f"Есть ошибки: {failures}. Исправьте и повторите mai.cmd check.")
        return 1
    say("Всё в порядке. Запуск: mai.cmd start")
    return 0


def _db_error(exc: Exception) -> str:
    """The server's message without the SQL text SQLAlchemy appends."""
    text_ = str(getattr(exc, "orig", None) or exc).strip()
    return re.sub(r"\s*\(SQL\w+\)\s*$", "", text_.split("\n[SQL:")[0])[:300]


def _check_mssql(values: dict[str, str]) -> str | None:
    """Driver, connection, version, JSON support, schema and rights of the MSSQL mode; a problem or None."""
    info = parse_mssql_url(values.get("MAI_DATABASE_URL", ""))
    if not info:
        return "в .env нет адреса SQL Server (MAI_DATABASE_URL): выполните mai.cmd setup → mssql"
    schema_option = schema_of(values)
    target = (f"{info['host']}" + (f",{info['port']}" if info["port"] else "") + f"/{info['name']}"
              + (f", схема {schema_option}" if schema_option else ""))
    drivers = odbc_drivers()
    if info["driver"] == PYMSSQL:
        ok("драйвер: pymssql (без ODBC-драйвера)")
    elif info["driver"] not in drivers:
        note("Установлены: " + (", ".join(drivers) or "ни одного ODBC-драйвера SQL Server"))
        note("Нужен «ODBC Driver 18 (или 17) for SQL Server» x64 (ставит администратор), выбрать")
        note("установленный или pymssql (без ODBC-драйвера): mai.cmd setup.")
        return f"ODBC-драйвер «{info['driver']}» не установлен"
    else:
        ok(f"ODBC-драйвер: {info['driver']}")
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    from app.db import connect_args

    engine = create_engine(values["MAI_DATABASE_URL"], poolclass=NullPool,
                           connect_args=connect_args(values["MAI_DATABASE_URL"], 15))
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)), SUSER_SNAME(), SCHEMA_NAME(), "
                "(SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME())")).one()
            version, login, default_schema, level = row
            ok(f"подключение к {target} (SQL Server {version}, вход {login})")
            if int(str(version).split(".")[0]) < 13:
                return "нужен SQL Server 2016 или новее (функции JSON)"
            if level is not None and int(level) < 130:
                note(f"Администратору: ALTER DATABASE [{info['name']}] SET COMPATIBILITY_LEVEL = 130  (или выше)")
                return f"уровень совместимости базы {level}: нужен 130 или выше (функции JSON)"
            schema = schema_option or default_schema
            if conn.execute(text("SELECT SCHEMA_ID(:schema)"), {"schema": schema}).scalar() is None:
                try:
                    conn.execute(text("DECLARE @sql nvarchar(400) = N'CREATE SCHEMA ' + QUOTENAME(:schema); "
                                      "EXEC sp_executesql @sql"), {"schema": schema})
                    conn.commit()
                    ok(f"создана схема {schema}")
                except Exception as exc:  # noqa: BLE001
                    note(f"Администратору: CREATE SCHEMA [{schema}] AUTHORIZATION [{login}];")
                    return f"схемы {schema} нет, создать её не удалось: {_db_error(exc)}"
            rights = conn.execute(text(
                "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE'), "
                "HAS_PERMS_BY_NAME(:schema, 'SCHEMA', 'ALTER')"), {"schema": schema}).one()
            if all(rights):
                ok(f"есть право создавать таблицы в схеме {schema}")
            else:
                # Enough when the tables are already there: the start then creates nothing.
                warn(f"нет права создавать таблицы в схеме {schema}: хватит, если таблицы уже созданы")
                note(f"Иначе администратору: GRANT CREATE TABLE TO [{login}]; GRANT ALTER ON SCHEMA::[{schema}] TO [{login}];")
    except Exception as exc:  # noqa: BLE001
        note("Проверьте сервер, порт, базу, вход и пароль (mai.cmd setup), доступ с этого компьютера к порту SQL")
        note("Server и сертификат («Доверять сертификату сервера» в mai.cmd setup).")
        return f"нет подключения к {target}: {_db_error(exc)}"
    finally:
        engine.dispose()
    return None


def _check_llm(settings) -> int:
    """Ping the chat model and, when set, the scan model with a test image; returns failures."""
    from app.core import llm

    failures = 0
    started = time.monotonic()
    try:
        answer = llm.chat([{"role": "system", "content": "Отвечай одним словом."},
                           {"role": "user", "content": "Напиши слово: готово"}],
                          model=settings.llm_model, timeout=min(settings.llm_timeout_seconds, 90), max_retries=0)
        ok(f"модель {settings.llm_model} отвечает за {time.monotonic() - started:.1f} с: «{answer.strip()[:40]}»")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        bad(f"модель {settings.llm_model} не отвечает: {exc}")
        _explain_llm(settings.llm_api_url, settings.llm_api_key, settings.llm_model)
    extraction = settings.extraction_model
    if extraction and extraction != settings.llm_model:
        try:
            llm.chat([{"role": "user", "content": "Напиши слово: готово"}], model=extraction, max_retries=0,
                     timeout=min(settings.llm_timeout_seconds, 90))
            ok(f"модель для документов {extraction} отвечает")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            bad(f"модель для документов {extraction} не отвечает: {exc}")
    if not settings.ocr_model:
        warn("модель для сканов не задана: сканы и фото не распознаются (PDF с текстом и Excel — работают)")
        return failures
    from PIL import Image, ImageDraw, ImageFont

    from app.core.documents import text as doc_text

    image = Image.new("RGB", (900, 260), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=90)
    except TypeError:  # Pillow before 10.1
        font = ImageFont.load_default()
    draw.text((40, 70), "MAI 4721", fill="black", font=font)
    started = time.monotonic()
    try:
        result = doc_text.ocr_image(image, "Проверка распознавания")
        if "4721" in result:
            ok(f"модель для сканов {settings.ocr_model} читает изображения ({time.monotonic() - started:.1f} с)")
        else:
            failures += 1
            bad(f"модель для сканов {settings.ocr_model} не прочитала тестовую картинку: «{result.strip()[:80]}»")
            note("Нужна модель, понимающая изображения (vision). Иначе оставьте поле пустым (mai.cmd setup).")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        bad(f"модель для сканов {settings.ocr_model} не принимает изображения: {exc}")
        note("Нужна модель, понимающая изображения (vision). Иначе оставьте поле пустым (mai.cmd setup).")
    return failures


def _explain_llm(url: str, api_key: str, model: str) -> None:
    """One raw request to show what LiteLLM answers (status and message)."""
    import requests

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = requests.post(url, headers=headers, timeout=60, json={
            "model": model, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 5})
        note(f"LiteLLM ответил: HTTP {response.status_code} {response.text.strip()[:300]}")
        if response.status_code in {401, 403}:
            note("Проверьте ключ LiteLLM (mai.cmd setup).")
        elif response.status_code in {400, 404}:
            note("Проверьте адрес (…/v1/chat/completions) и имя модели в LiteLLM.")
    except requests.RequestException as exc:
        note(f"Соединения с {url} нет: {type(exc).__name__}. Проверьте адрес и сеть; для внешнего адреса —")
        note("прокси (HTTPS_PROXY в .env).")
