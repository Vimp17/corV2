"""mai.cmd setup: the settings wizard (SQL Server, LiteLLM, data, access) → .env."""
from __future__ import annotations

import getpass
import secrets
import shutil
import socket
from datetime import date
from urllib.parse import urlsplit

from standalone import envfile
from standalone.cli.common import (build_mssql_url, dashboard_link, DATA_DIRS, DEFAULTS, ENV_FILE, env_values,
                                   MSSQL_SCHEMA_NAME, normalize_llm_url, note, OBSOLETE_SETTINGS, odbc_drivers,
                                   parse_date, parse_mssql_url, PYMSSQL, ROOT, say, schema_of, Stop, TEMPLATE,
                                   warn)


# --------------------------------------------------------------------------- setup
def _ask(question: str, default: str = "", *, secret: bool = False, interactive: bool = True,
         preset: str | None = None, check=None, shown_default: str | None = None) -> str:
    """One answer: the preset (command-line option) wins, else the prompt, else the default."""
    if preset is not None:
        value = preset
    elif not interactive:
        value = default
    else:
        hint = shown_default if shown_default is not None else default
        prompt = f"  {question}" + (f" [{hint}]" if hint else "") + ": "
        while True:
            try:
                value = getpass.getpass(prompt) if secret else input(prompt)
            except EOFError:
                value = ""
            value = value.strip() or default
            problem = check(value) if check else None
            if not problem:
                break
            say(f"        {problem}")
        return value
    problem = check(value) if check else None
    if problem:
        raise Stop(f"{question}: {problem}")
    return value


def _yes(value: str) -> bool:
    return value.strip().lower() in {"да", "д", "yes", "y", "1", "true"}


def _check_port(value: str) -> str | None:
    return None if value.isdigit() and 1 <= int(value) <= 65535 else "нужен номер порта от 1 до 65535"


def _check_mssql_schema(value: str) -> str | None:
    return None if not value or MSSQL_SCHEMA_NAME.match(value) else "латиница, цифры и _"


def _check_timezone(value: str) -> str | None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return "неизвестный часовой пояс, например Europe/Moscow или Asia/Yekaterinburg"
    return None


def _check_date(value: str) -> str | None:
    if not value:
        return None
    when = parse_date(value)
    if when is None:
        return "формат ДД.ММ.ГГГГ"
    return "дата в будущем" if when > date.today() else None


def _required(value: str) -> str | None:
    return None if value else "обязательное поле"


def cmd_setup(args) -> int:
    interactive = not args.non_interactive
    old = env_values() if ENV_FILE.is_file() else {}
    say("Настройка демо-сборки MAI Corrosion Control. Enter — оставить значение в [скобках].")

    say("\n1/4  Хранилище приложения — Microsoft SQL Server (не подключение внешнего источника)")
    db_url, mssql_schema = _setup_mssql(args, old, old.get("MAI_DATABASE_URL", "").startswith("mssql"), interactive)

    say("\n2/4  Языковая модель через LiteLLM (предложения ИИ-агента, документы)")
    llm_url = _ask("Адрес LiteLLM, например http://litellm.corp:4000 (пусто — без ИИ)",
                   old.get("MAI_LLM_API_URL", ""), interactive=interactive, preset=args.llm_url)
    llm_url = normalize_llm_url(llm_url)
    llm_key = llm_model = ocr_model = ""
    if llm_url:
        llm_key = _ask("Ключ LiteLLM (не отображается)", old.get("MAI_LLM_API_KEY", ""), secret=True,
                       interactive=interactive, preset=args.llm_key,
                       shown_default="оставить прежний" if old.get("MAI_LLM_API_KEY") else "")
        llm_model = _ask("Модель (имя в LiteLLM)", old.get("MAI_LLM_MODEL", ""), interactive=interactive,
                         preset=args.llm_model, check=_required)
        previous_ocr = old.get("MAI_OCR_MODEL", llm_model) or "-"
        ocr_model = _ask("Модель для сканов и фото — должна понимать изображения (- = сканы не распознаются)",
                         previous_ocr, interactive=interactive, preset=args.ocr_model)
        ocr_model = "" if ocr_model.strip() in {"-", ""} else ocr_model.strip()

    say("\n3/4  Данные")
    timezone_name = _ask("Часовой пояс дат в файлах", old.get("MAI_SOURCE_TIMEZONE") or DEFAULTS["timezone"],
                         interactive=interactive, preset=args.timezone, check=_check_timezone)
    previous_date = parse_date(old.get("MAI_STANDALONE_DATA_DATE", ""))
    data_date = _ask("Дата последних данных в выгрузках, ДД.ММ.ГГГГ (пусто — данные текущие; - = убрать)",
                     previous_date.strftime("%d.%m.%Y") if previous_date else "", interactive=interactive,
                     preset=args.data_date, check=lambda v: None if v.strip() == "-" else _check_date(v))
    data_date = "" if data_date.strip() == "-" else data_date
    if data_date:
        data_date = parse_date(data_date).isoformat()

    say("\n4/4  Доступ")
    was_network = old.get("MAI_STANDALONE_HOST", "0.0.0.0") not in {"127.0.0.1", "localhost"}
    preset_network = None if args.network is None else ("да" if args.network else "нет")
    network = _yes(_ask("Открывать дашборд по ссылке с других компьютеров и из n8n? да/нет",
                        "да" if was_network else "нет", interactive=interactive, preset=preset_network))
    api_port = _ask("Порт дашборда и API", old.get("MAI_STANDALONE_PORT") or DEFAULTS["port"],
                    interactive=interactive, preset=args.port, check=_check_port)
    if network:
        default_host = urlsplit(old.get("MAI_STANDALONE_PUBLIC_URL", "")).hostname or _server_name()
        public_host = _ask("Имя или IP этого сервера для ссылки", default_host, interactive=interactive,
                           preset=args.public_host, check=_required)
    else:
        public_host = "localhost"
    key = old.get("MAI_API_KEY", "")
    if args.api_key is not None:
        key = "" if args.api_key == "none" else (secrets.token_urlsafe(24) if args.api_key == "new" else args.api_key)
    elif network:
        choice = _ask("Ключ доступа: новый / оставить / нет", "оставить" if key else "новый",
                      interactive=interactive,
                      check=lambda v: None if v in {"новый", "оставить", "нет"} else "новый, оставить или нет")
        key = secrets.token_urlsafe(24) if choice == "новый" or (choice == "оставить" and not key) else (
            "" if choice == "нет" else key)
    else:
        key = ""

    # Settings of the database modes earlier builds had (built-in PostgreSQL, SQLite) are dropped.
    values = {name_: value for name_, value in old.items() if name_ not in OBSOLETE_SETTINGS}
    values.update({
        "MAI_DATABASE_URL": db_url, "MAI_DATABASE_SCHEMA": mssql_schema,
        "MAI_LLM_API_URL": llm_url, "MAI_LLM_API_KEY": llm_key, "MAI_LLM_MODEL": llm_model,
        "MAI_OCR_MODEL": ocr_model, "MAI_SOURCE_TIMEZONE": timezone_name, "MAI_STANDALONE_DATA_DATE": data_date,
        "MAI_STANDALONE_HOST": "0.0.0.0" if network else "127.0.0.1", "MAI_STANDALONE_PORT": api_port,
        "MAI_STANDALONE_PUBLIC_URL": f"http://{public_host}:{api_port}",
        "MAI_DASHBOARD_URL": f"http://{public_host}:{api_port}/dashboard", "MAI_API_KEY": key,
    })
    template_values = envfile.read(TEMPLATE)
    for name_, default in template_values.items():
        values.setdefault(name_, default)
    if ENV_FILE.is_file():
        shutil.copy2(ENV_FILE, ENV_FILE.with_name(".env.bak"))
    ENV_FILE.write_text(envfile.render(TEMPLATE, values), encoding="utf-8")
    for folder in DATA_DIRS:
        folder.mkdir(parents=True, exist_ok=True)

    say("\nГотово: настройки записаны в .env" + (" (прежние — в .env.bak)." if (ROOT / ".env.bak").is_file() else "."))
    if not llm_url:
        warn("Языковая модель не задана: предложения ИИ-агента, распознавание PDF и сканов работать не будут.")
    if network:
        say(f"\nПорт {api_port} должен быть открыт в брандмауэре Windows для входящих подключений — это делает")
        say("администратор (см. README-DEMO.md, «Что попросить у администраторов»).")
        if key:
            say("Ссылка для демонстрации (после запуска; ключ в ссылке — не пересылайте её посторонним):")
            say(f"  {dashboard_link(values)}")
    say("\nДальше:  mai.cmd check   — проверить базу (и создать таблицы), модель и порт")
    say("         mai.cmd start   — запустить")
    if interactive and _yes(_ask("Настроить источники сейчас? да/нет", "нет")):
        from argparse import Namespace
        from standalone.cli.sources import cmd_sources
        cmd_sources(Namespace(action="configure", source=None, url=None, token=None, well_ids=None,
                              non_interactive=False))
    else:
        say("Источники можно настроить позже: mai.cmd sources configure")
    return 0


def _setup_mssql(args, old: dict[str, str], was_mssql: bool, interactive: bool) -> tuple[str, str]:
    """Questions of the MSSQL mode; returns (MAI_DATABASE_URL, schema)."""
    db = parse_mssql_url(old.get("MAI_DATABASE_URL", "")) if was_mssql else {}
    drivers = odbc_drivers()
    note("SQL Server 2016 или новее. Адрес, база, схема и вход — от администраторов (README-DEMO.md, раздел 4).")
    if drivers:
        note("ODBC-драйверы на этом компьютере: " + ", ".join(drivers) + "; без ODBC-драйвера — pymssql.")
    else:
        note("ODBC-драйвер SQL Server не найден — подключение через pymssql (входит в сборку, ничего ставить")
        note("не нужно). Если нужен ODBC: «ODBC Driver 18 (или 17) for SQL Server» x64 ставит администратор.")
    host = _ask("Сервер (имя или IP; именованный экземпляр — сервер\\ИМЯ)", db.get("host", ""),
                interactive=interactive, preset=args.db_host, check=_required)
    port = _ask("Порт (- = без порта: именованный экземпляр с динамическим портом)",
                db.get("port") if db else DEFAULTS["mssql_port"], interactive=interactive, preset=args.db_port,
                check=lambda v: None if v.strip() == "-" else _check_port(v))
    port = "" if port.strip() == "-" else port
    name = _ask("База данных", db.get("name", ""), interactive=interactive, preset=args.db_name, check=_required)
    schema = _ask("Схема (пусто — схема по умолчанию для входа, обычно dbo)",
                  schema_of(old) if was_mssql else "", interactive=interactive,
                  preset=args.db_schema, check=_check_mssql_schema)
    auth = _ask("Вход: sql (логин и пароль SQL Server) / windows (учётная запись Windows, под которой запущена сборка)",
                "windows" if db.get("windows") else "sql", interactive=interactive, preset=args.db_auth,
                check=lambda v: None if v in {"sql", "windows"} else "sql или windows")
    user = password = ""
    if auth == "sql":
        user = _ask("Логин", db.get("user", ""), interactive=interactive, preset=args.db_user, check=_required)
        password = _ask("Пароль (не отображается)", db.get("password", ""), secret=True, interactive=interactive,
                        preset=args.db_password, shown_default="оставить прежний" if db.get("password") else "")
    driver = _ask(f"Драйвер: {' / '.join(drivers + [PYMSSQL])} (pymssql — без ODBC-драйвера)",
                  db.get("driver") or (drivers[0] if drivers else PYMSSQL), interactive=interactive,
                  preset=args.db_driver, check=_required)
    trust = True
    if driver != PYMSSQL:
        trust = _yes(_ask("Доверять сертификату сервера без проверки? да — если сертификат самоподписанный "
                          "(обычно во внутренней сети) / нет", "да" if db.get("trust", True) else "нет",
                          interactive=interactive, preset=args.db_trust_cert))
    elif auth == "windows":
        warn("Вход Windows через pymssql зависит от настроек домена; если не пройдёт — вход sql или ODBC-драйвер.")
    return build_mssql_url(host, port, name, user, password, driver, trust), schema


def _server_name() -> str:
    for name in (socket.getfqdn(), socket.gethostname()):
        if name and not name.startswith("localhost"):
            return name
    return "localhost"
