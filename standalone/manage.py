"""Управление демо-сборкой MAI Corrosion Control без Docker.

Windows: mai.cmd <команда>        Linux: python standalone/manage.py <команда>

  setup     мастер настройки: PostgreSQL, языковая модель (LiteLLM), данные, доступ → файл .env
  check     проверка перед запуском: пакеты, база данных (и создание таблиц), модель, порт
  start     запустить в фоне (журнал logs/api.log)
  stop      остановить                       restart   перезапустить
  status    состояние и ссылки               run       запустить в этом окне (Ctrl+C — остановить)
  logs      последние строки журнала (-f — следить за журналом)
  link      ссылка на дашборд с ключом доступа
  n8n       сценарии n8n с адресом этого сервера → папка n8n-demo
  cleanup   после демонстрации: удалить таблицы демо и загруженные файлы
  version   версия сборки

Подробно — README-DEMO.md в корне сборки.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from standalone import envfile  # noqa: E402

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
ENTRY = "standalone.asgi:app"
WINDOWS = os.name == "nt"
LOG_ROTATE_BYTES = 20 * 1024 * 1024
START_TIMEOUT_SECONDS = 180
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
DEFAULTS = {"port": "8000", "timezone": "Europe/Moscow", "sslmode": "prefer", "db_port": "5432"}


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


def build_db_url(host: str, port: str, name: str, user: str, password: str, schema: str, sslmode: str) -> str:
    query = {}
    if schema:
        query["options"] = f"-csearch_path={schema}"
    if sslmode:
        query["sslmode"] = sslmode
    auth = quote(user, safe="") + (":" + quote(password, safe="") if password else "")
    host_part = f"[{host}]" if ":" in host else host
    url = f"postgresql+psycopg://{auth}@{host_part}:{port}/{quote(name, safe='')}"
    return url + ("?" + urlencode(query, safe="") if query else "")


def parse_db_url(url: str) -> dict[str, str]:
    if not url:
        return {}
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    options = query.get("options", "")
    match = re.search(r"search_path=([^\s,]+)", options)
    return {"host": parts.hostname or "", "port": str(parts.port or DEFAULTS["db_port"]),
            "name": unquote(parts.path.lstrip("/")), "user": unquote(parts.username or ""),
            "password": unquote(parts.password or ""), "schema": match.group(1) if match else "",
            "sslmode": query.get("sslmode", "")}


def libpq_url(url: str) -> str:
    """The SQLAlchemy URL as a libpq connection URI (for psycopg.connect)."""
    return re.sub(r"^postgresql\+psycopg://", "postgresql://", url)


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


def _check_schema(value: str) -> str | None:
    return None if not value or SCHEMA_NAME.match(value) else "латиница в нижнем регистре, цифры и _"


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
    db = parse_db_url(old.get("MAI_DATABASE_URL", ""))
    say("Настройка демо-сборки MAI Corrosion Control. Enter — оставить значение в [скобках].")

    say("\n1/4  База данных PostgreSQL (адрес, база или схема и пользователь — от администраторов)")
    host = _ask("Сервер (имя или IP)", db.get("host") or "localhost", interactive=interactive,
                preset=args.db_host, check=_required)
    port = _ask("Порт", db.get("port") or DEFAULTS["db_port"], interactive=interactive, preset=args.db_port,
                check=_check_port)
    name = _ask("База данных", db.get("name", ""), interactive=interactive, preset=args.db_name, check=_required)
    schema = _ask("Схема (если выделили схему в общей базе; пусто — своя база, схема public)", db.get("schema", ""),
                  interactive=interactive, preset=args.db_schema, check=_check_schema)
    user = _ask("Пользователь", db.get("user", ""), interactive=interactive, preset=args.db_user, check=_required)
    password = _ask("Пароль (не отображается)", db.get("password", ""), secret=True, interactive=interactive,
                    preset=args.db_password, shown_default="оставить прежний" if db.get("password") else "")
    sslmode = _ask("SSL: disable / prefer / require", db.get("sslmode") or DEFAULTS["sslmode"],
                   interactive=interactive, preset=args.db_sslmode,
                   check=lambda v: None if v in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
                   else "disable, prefer или require")

    say("\n2/4  Языковая модель через LiteLLM (справка ИИ, предложение агента, документы)")
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

    values = dict(old)
    values.update({
        "MAI_DATABASE_URL": build_db_url(host, port, name, user, password, schema, sslmode),
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
        warn("Языковая модель не задана: справка и предложение ИИ, распознавание PDF и сканов работать не будут.")
    if network:
        say(f"\nПорт {api_port} должен быть открыт в брандмауэре Windows для входящих подключений — это делает")
        say("администратор (см. README-DEMO.md, «Что попросить у администраторов»).")
        if key:
            say("Ссылка для демонстрации (после запуска; ключ в ссылке — не пересылайте её посторонним):")
            say(f"  {dashboard_link(values)}")
    say("\nДальше:  mai.cmd check   — проверить базу, модель и порт")
    say("         mai.cmd start   — запустить")
    return 0


def _server_name() -> str:
    for name in (socket.getfqdn(), socket.gethostname()):
        if name and not name.startswith("localhost"):
            return name
    return "localhost"


# --------------------------------------------------------------------------- process
def _http_status(url: str, timeout: float = 3.0) -> tuple[int | None, str]:
    """GET without any proxy (the API is local); (status, body) or (None, error)."""
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status, response.read(4000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4000).decode("utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        return None, str(exc)


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _PROCESS_TERMINATE, _PROCESS_QUERY = 0x0001, 0x1000
    _STILL_ACTIVE, _SYNCHRONIZE = 259, 0x00100000


def _alive(pid: int | None) -> bool:
    """The process runs and is our API (python running uvicorn), not a reused process id."""
    if not pid:
        return False
    if WINDOWS:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
                return False
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value.lower().endswith(("python.exe", "pythonw.exe"))
            return True
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return "uvicorn" in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return True


def _terminate(pid: int) -> None:
    if WINDOWS:
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_QUERY | _SYNCHRONIZE, False, pid)
        if handle:
            try:
                _kernel32.TerminateProcess(handle, 1)
                _kernel32.WaitForSingleObject(handle, 15000)
            finally:
                _kernel32.CloseHandle(handle)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(150):
        if not _alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _pid_on_port(port: int) -> int | None:
    """Windows: the process listening on the port (when the pid file was lost)."""
    if not WINDOWS:
        return None
    try:
        output = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.decode("ascii", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[2].endswith((":0", ":*")) and parts[-1].isdigit():
            return int(parts[-1])
    return None


def _api_command(values: dict[str, str]) -> list[str]:
    return [sys.executable, "-X", "utf8", "-m", "uvicorn", ENTRY, "--host", bind_host(values),
            "--port", str(port_of(values)), "--timeout-graceful-shutdown", "10"]


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def _running(values: dict[str, str]) -> tuple[int | None, bool]:
    """(pid of our process if alive, whether the API answers on its port)."""
    pid = _read_pid()
    pid = pid if _alive(pid) else None
    status, _ = _http_status(f"{local_url(values)}/health/live", timeout=2)
    return pid, status == 200


def _rotate_log() -> None:
    try:
        if LOG_FILE.stat().st_size > LOG_ROTATE_BYTES:
            LOG_FILE.replace(LOG_FILE.with_name("api.log.1"))
    except OSError:
        pass


def _tail(lines: int) -> list[str]:
    try:
        data = LOG_FILE.read_bytes()[-200_000:]
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()[-lines:]


def _port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if not WINDOWS:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def cmd_start(args) -> int:
    values = require_env()
    pid, answering = _running(values)
    if pid or answering:
        say("Уже запущено.")
        return cmd_status(args)
    if not _port_free(bind_host(values), port_of(values)):
        raise Stop(f"Порт {port_of(values)} занят другой программой. Выберите другой: mai.cmd setup (вопрос «Порт»).")
    for folder in DATA_DIRS:
        folder.mkdir(parents=True, exist_ok=True)
    _rotate_log()
    with LOG_FILE.open("ab") as log:
        log.write(f"\n===== start {datetime.now():%Y-%m-%d %H:%M:%S} =====\n".encode())
        log.flush()
        options = {"cwd": str(ROOT), "env": _child_env(), "stdin": subprocess.DEVNULL, "stdout": log,
                   "stderr": subprocess.STDOUT, "close_fds": True}
        if WINDOWS:
            detached, new_group, breakaway = 0x00000008, 0x00000200, 0x01000000
            try:
                process = subprocess.Popen(_api_command(values), creationflags=detached | new_group | breakaway, **options)
            except OSError:  # the console's job object may forbid breaking away
                process = subprocess.Popen(_api_command(values), creationflags=detached | new_group, **options)
        else:
            process = subprocess.Popen(_api_command(values), start_new_session=True, **options)
    PID_FILE.write_text(str(process.pid), encoding="ascii")
    say(f"Запуск (журнал: {LOG_FILE.relative_to(ROOT)})", )
    started = time.monotonic()
    while time.monotonic() - started < START_TIMEOUT_SECONDS:
        if process.poll() is not None:
            PID_FILE.unlink(missing_ok=True)
            say("\nПроцесс завершился при запуске. Последние строки журнала:")
            for line in _tail(25):
                say("  " + line)
            raise Stop("Не запустилось. Частые причины — в README-DEMO.md, раздел «Неполадки»; проверка: mai.cmd check")
        status, body = _http_status(f"{local_url(values)}/health/ready", timeout=3)
        if status == 200:
            say(f"\nЗапущено за {time.monotonic() - started:.0f} с.")
            return cmd_status(args)
        if status == 503:
            warn("API запущен, но не видит базу данных: " + body[:200])
        print(".", end="", flush=True)
        time.sleep(2)
    say("")
    warn(f"API не ответил за {START_TIMEOUT_SECONDS} с. Процесс оставлен работать; смотрите mai.cmd logs.")
    return 1


def cmd_stop(args) -> int:
    values = env_values()
    pid = _read_pid()
    if not _alive(pid):
        pid = None
        if values and _running(values)[1]:
            pid = _pid_on_port(port_of(values))
            if pid is None:
                raise Stop("API отвечает, но его процесс не найден (файл run/api.pid потерян). "
                           "Закройте python.exe в Диспетчере задач.")
    if pid is None:
        PID_FILE.unlink(missing_ok=True)
        say("Не запущено.")
        return 0
    _terminate(pid)
    PID_FILE.unlink(missing_ok=True)
    say("Остановлено." if not _alive(pid) else "Не удалось остановить процесс — закройте python.exe в Диспетчере задач.")
    return 0


def cmd_restart(args) -> int:
    cmd_stop(args)
    time.sleep(1)
    return cmd_start(args)


def cmd_status(args) -> int:
    values = require_env()
    pid, answering = _running(values)
    if not pid and not answering:
        say("Не запущено.  Запуск: mai.cmd start")
        return 3
    status, body = _http_status(f"{local_url(values)}/health/ready", timeout=5)
    say("Работает" + (f" (процесс {pid})" if pid else " (процесс запущен не этой командой)") + ".")
    if status == 200:
        try:
            ready = json.loads(body)
        except ValueError:
            ready = {}
        ok("база данных подключена")
        llm = ready.get("llm_agent") == "configured"
        (ok if llm else warn)("языковая модель " + ("настроена" if llm else "не настроена"))
        ocr = ready.get("ocr_model") == "configured"
        (ok if ocr else warn)("модель для сканов " + ("настроена" if ocr else "не настроена — сканы не распознаются"))
    else:
        warn(f"API отвечает, но не готов: {body[:200]}")
    say(f"\nНа этом сервере:  {local_url(values)}/dashboard")
    if bind_host(values) not in {"127.0.0.1", "localhost"}:
        say(f"Ссылка для других: {dashboard_link(values)}")
    return 0


def cmd_run(args) -> int:
    values = require_env()
    pid, answering = _running(values)
    if pid or answering:
        raise Stop("Уже запущено в фоне. Остановите: mai.cmd stop")
    for folder in DATA_DIRS:
        folder.mkdir(parents=True, exist_ok=True)
    say(f"Запуск в этом окне. Дашборд: {local_url(values)}/dashboard   (Ctrl+C — остановить)")
    try:
        return subprocess.call(_api_command(values), cwd=str(ROOT), env=_child_env())
    except KeyboardInterrupt:
        return 0


def cmd_logs(args) -> int:
    for line in _tail(args.lines):
        say(line)
    if not args.follow:
        return 0
    position = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    try:
        while True:
            time.sleep(1)
            try:
                size = LOG_FILE.stat().st_size
            except OSError:
                continue
            if size < position:
                position = 0
            if size > position:
                with LOG_FILE.open("rb") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                position = size
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
    except KeyboardInterrupt:
        return 0


def cmd_link(args) -> int:
    values = require_env()
    say(dashboard_link(values))
    if not values.get("MAI_API_KEY"):
        note("Ключ доступа не задан: дашборд и данные открыты всем, кто знает адрес.")
    return 0


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
    modules = ("numpy", "pandas", "scipy", "lifelines", "psycopg", "sqlalchemy", "fastapi", "uvicorn",
               "openpyxl", "pypdfium2", "PIL", "requests", "alembic", "tzdata")
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
    db = parse_db_url(values["MAI_DATABASE_URL"])
    target = f"{db.get('host')}:{db.get('port')}/{db.get('name')}" + (f", схема {db['schema']}" if db.get("schema") else "")
    try:
        import psycopg

        with psycopg.connect(libpq_url(values["MAI_DATABASE_URL"]), connect_timeout=10, autocommit=True) as conn:
            version = conn.execute("SHOW server_version").fetchone()[0]
            user = conn.execute("SELECT current_user").fetchone()[0]
            ok(f"подключение к {target} (PostgreSQL {version}, пользователь {user})")
            if int(version.split(".")[0]) < 12:
                warn("PostgreSQL старше 12 не проверялся — возможны ошибки миграций")
            schema = db.get("schema") or "public"
            exists = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone()
            if not exists:
                try:
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    ok(f"создана схема {schema}")
                except psycopg.Error as exc:
                    fail(f"схемы {schema} нет, создать её не удалось: {str(exc).strip()[:200]}")
                    note("Попросите администратора создать схему и дать на неё права пользователю.")
                    return 1
            can_create = conn.execute("SELECT has_schema_privilege(current_user, %s, 'CREATE')", (schema,)).fetchone()[0]
            if not can_create:
                fail(f"нет права создавать таблицы в схеме {schema}")
                note(f"Нужно: GRANT USAGE, CREATE ON SCHEMA {schema} TO {user};  (делает администратор БД)")
                return 1
            ok(f"есть право создавать таблицы в схеме {schema}")
    except Exception as exc:  # noqa: BLE001
        fail(f"нет подключения к {target}: {str(exc).strip()[:300]}")
        note("Проверьте адрес, порт, имя базы, пользователя и пароль (mai.cmd setup) и доступ с этого сервера к порту БД.")
        return 1

    prepared = _prepare_app_environment()
    from app import db as app_db
    from app.config import settings

    try:
        app_db.init_db()
        with app_db.engine().connect() as conn:
            from sqlalchemy import text

            tables = conn.execute(text("SELECT count(*) FROM information_schema.tables "
                                       "WHERE table_schema = current_schema()")).scalar()
        ok(f"таблицы приложения созданы или обновлены (в схеме {tables} таблиц)")
    except Exception as exc:  # noqa: BLE001
        fail(f"создать таблицы не удалось: {str(exc).strip()[:300]}")
        return 1

    say("\nЯзыковая модель")
    if not (settings.llm_api_url and settings.llm_model):
        warn("не настроена: справка и предложение ИИ, распознавание PDF и сканов не работают")
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

    say("\nОграничения демо-сборки")
    ok(f"запросы к источникам выключены ({len(prepared['cleared'])} адресов очищено), отправка в Промайн выключена")
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
    say("Будут удалены: все таблицы демо-сборки в базе данных (только таблицы этого приложения),")
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
        with app_db.engine().begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        ok("таблицы удалены")
    except Exception as exc:  # noqa: BLE001
        bad(f"таблицы удалить не удалось: {str(exc).strip()[:300]}")
        return 1
    schema = parse_db_url(values["MAI_DATABASE_URL"]).get("schema")
    if schema and args.drop_schema:
        try:
            with app_db.engine().begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}"'))
            ok(f"схема {schema} удалена")
        except Exception as exc:  # noqa: BLE001
            warn(f"схему {schema} удалить не удалось (в ней есть чужие объекты?): {str(exc).strip()[:200]}")
    for folder in (ROOT / "data" / "documents", ROOT / "inbox", LOG_DIR, N8N_OUT):
        if not folder.exists():
            continue
        for item in folder.iterdir():
            if item.name == ".gitkeep":
                continue
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
    ok("файлы удалены")
    say("\nГотово. Чтобы удалить сборку полностью — удалите папку целиком.")
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


# --------------------------------------------------------------------------- main
def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="mai.cmd", description="Демо-сборка MAI Corrosion Control без Docker.",
                                   formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    commands = main.add_subparsers(dest="command")
    setup = commands.add_parser("setup", help="мастер настройки → .env")
    setup.add_argument("--non-interactive", action="store_true", help="не задавать вопросов: значения из параметров и .env")
    for option in ("db-host", "db-port", "db-name", "db-schema", "db-user", "db-password", "db-sslmode", "llm-url",
                   "llm-key", "llm-model", "ocr-model", "timezone", "data-date", "port", "public-host"):
        setup.add_argument(f"--{option}")
    setup.add_argument("--network", dest="network", action="store_true", default=None, help="доступ по сети")
    setup.add_argument("--local", dest="network", action="store_false", help="только с этого сервера")
    setup.add_argument("--api-key", help="new — создать, none — без ключа, иначе — сам ключ")
    commands.add_parser("check", help="проверка: пакеты, база, модель, порт")
    commands.add_parser("start", help="запустить в фоне")
    commands.add_parser("stop", help="остановить")
    commands.add_parser("restart", help="перезапустить")
    commands.add_parser("status", help="состояние и ссылки")
    commands.add_parser("run", help="запустить в этом окне")
    logs = commands.add_parser("logs", help="журнал")
    logs.add_argument("-n", "--lines", type=int, default=60)
    logs.add_argument("-f", "--follow", action="store_true")
    commands.add_parser("link", help="ссылка на дашборд с ключом")
    n8n = commands.add_parser("n8n", help="сценарии n8n с адресом этого сервера")
    n8n.add_argument("--api-url", help="адрес API, как его видит n8n (по умолчанию — адрес из setup)")
    n8n.add_argument("--prefix", default="[ДЕМО] ", help="приставка к названиям сценариев")
    cleanup = commands.add_parser("cleanup", help="удалить данные демо")
    cleanup.add_argument("--yes", action="store_true", help="без подтверждения")
    cleanup.add_argument("--drop-schema", action="store_true", help="удалить и пустую схему")
    commands.add_parser("version", help="версия сборки")
    return main


def main(argv: list[str] | None = None) -> int:
    os.chdir(ROOT)
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0].lower() in {"help", "/?", "-?"}:
        argv = ["--help"]
    args = parser().parse_args(argv)
    handlers = {"setup": cmd_setup, "check": cmd_check, "start": cmd_start, "stop": cmd_stop,
                "restart": cmd_restart, "status": cmd_status, "run": cmd_run, "logs": cmd_logs, "link": cmd_link,
                "n8n": cmd_n8n, "cleanup": cmd_cleanup, "version": cmd_version}
    if not args.command:
        parser().print_help()
        return 0
    try:
        return handlers[args.command](args) or 0
    except Stop as exc:
        say(f"\n{exc}")
        return 1
    except KeyboardInterrupt:
        say("\nПрервано.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
