"""The API process: start, stop, restart, status, run, logs, link."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from standalone.cli.common import (bind_host, dashboard_link, DATA_DIRS, database_target, ENTRY, env_values,
                                   local_url, LOG_FILE, LOG_ROTATE_BYTES, note, ok, PID_FILE, port_of,
                                   require_env, ROOT, say, START_TIMEOUT_SECONDS, Stop, warn, WINDOWS)


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
        say("API не запущен.")
    else:
        _terminate(pid)
        PID_FILE.unlink(missing_ok=True)
        say("API остановлен." if not _alive(pid) else
            "Не удалось остановить API — закройте python.exe в Диспетчере задач.")
    return 0


def cmd_restart(args) -> int:
    cmd_stop(args)
    time.sleep(1)
    return cmd_start(args)


def cmd_status(args) -> int:
    values = require_env()
    pid, answering = _running(values)
    note(f"база: SQL Server {database_target(values)}")
    if not pid and not answering:
        say("API не запущен.  Запуск: mai.cmd start")
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
