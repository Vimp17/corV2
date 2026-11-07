"""Управление демо-сборкой MAI Corrosion Control без Docker.

Windows: mai.cmd <команда>        Linux: python standalone/manage.py <команда>

  setup     мастер настройки: база данных SQL Server, LiteLLM, данные, доступ → .env
  check     проверка перед запуском: пакеты, база данных (и создание таблиц), модель, порт
  start     запустить в фоне (журналы в logs/)
  stop      остановить                       restart   перезапустить
  status    состояние и ссылки               run       запустить в этом окне (Ctrl+C — остановить)
  logs      последние строки журнала (-f — следить за журналом)
  link      ссылка на дашборд с ключом доступа
  n8n       сценарии n8n с адресом этого сервера → папка n8n-demo
  cleanup   после демонстрации: удалить таблицы демо и загруженные файлы
  diag      отчёт для разработчика: версия, проверка, состояние, журналы (пароли и ключи скрыты)
  version   версия сборки

Подробно — README-DEMO.md в корне сборки.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from standalone.cli.common import say, Stop  # noqa: E402
from standalone.cli.wizard import cmd_setup  # noqa: E402
from standalone.cli.service import (cmd_link, cmd_logs, cmd_restart, cmd_run, cmd_start,  # noqa: E402
                                     cmd_status, cmd_stop)
from standalone.cli.check import cmd_check  # noqa: E402
from standalone.cli.tools import cmd_cleanup, cmd_diag, cmd_n8n, cmd_version  # noqa: E402


# --------------------------------------------------------------------------- main
def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="mai.cmd", description="Демо-сборка MAI Corrosion Control без Docker.",
                                   formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    commands = main.add_subparsers(dest="command")
    setup = commands.add_parser("setup", help="мастер настройки → .env")
    setup.add_argument("--non-interactive", action="store_true", help="не задавать вопросов: значения из параметров и .env")
    for option in ("db-host", "db-port", "db-name", "db-schema", "db-user", "db-password",
                   "db-auth", "db-driver", "db-trust-cert", "llm-url", "llm-key", "llm-model", "ocr-model", "timezone",
                   "data-date", "port", "public-host"):
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
    diag = commands.add_parser("diag", help="отчёт для разработчика (пароли и ключи скрыты)")
    diag.add_argument("-n", "--lines", type=int, default=300, help="строк журнала приложения")
    diag.add_argument("--no-check", action="store_true", help="без проверки базы и модели (быстрее)")
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
                "n8n": cmd_n8n, "cleanup": cmd_cleanup, "diag": cmd_diag, "version": cmd_version}
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
