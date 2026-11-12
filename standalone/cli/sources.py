"""Source connection setup and read-only checks. No database is needed for either command."""
from __future__ import annotations

import os
import shutil

from standalone import envfile
from standalone.cli.common import ENV_FILE, TEMPLATE, Stop, bad, note, ok, say, warn
from standalone.cli.wizard import _ask


def _single_line(value: str) -> str | None:
    return "значение должно занимать одну строку" if any(c in value for c in "\r\n\0") else None


def _wells(value: str) -> str:
    ids = list(dict.fromkeys(w.strip() for w in value.split(",") if w.strip()))
    if any(len(w) > 128 for w in ids) or _single_line(value):
        raise Stop("ID скважины: от 1 до 128 символов; несколько ID разделяйте запятой.")
    return ",".join(ids)


def cmd_sources(args) -> int:
    from app.core.source_connectors import check_source, load_profiles, source_setting, source_url_problem

    profiles = load_profiles()
    if args.source and args.source not in profiles:
        raise Stop("Неизвестный источник. Доступны: " + ", ".join(profiles))
    selected = {key: p for key, p in profiles.items() if not args.source or key == args.source}
    values = envfile.read(ENV_FILE)
    if args.action == "check":
        well = (args.well or values.get("MAI_WELL_IDS", "").split(",")[0]).strip()
        if not well or len(well) > 128 or _single_line(well):
            raise Stop("Укажите ID: mai.cmd sources check --well <ID>. Проверка ничего не записывает в БД.")
        previous = {key: os.environ.get(key) for key in values}
        os.environ.update(values)
        failures = 0
        try:
            for profile_id, profile in selected.items():
                source = str(profile.get("source_id", profile_id)).upper()
                title = profile.get("short_title") or profile.get("title") or source
                if profile.get("pull") is False or not profile.get("adapter") and not os.getenv(source_setting(source, "URL")):
                    note(f"{title}: загрузка файлов / push API; автоматическое подключение не задано.")
                    continue
                report = check_source(profile_id, well)
                output = ok if report["status"] == "ok" else warn if report["status"] == "warning" else bad
                failures += report["status"] == "error"
                output(f"{title}: {report['message']}")
                if report.get("record_count"):
                    note(f"Записей: {report['record_count']}; лист: {report.get('sheet_name', 'JSON')}.")
                if report.get("source_columns"):
                    note("Колонки: " + "; ".join(f"{key} ← {value}" for key, value in report["source_columns"].items()))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return int(bool(failures))

    interactive = not args.non_interactive
    if not args.source and any(v is not None for v in (args.url, args.token)):
        raise Stop("Для URL/токена выберите один источник: --source dars / khimik / era / crew_schedule.")
    say("Источники: прямой GET→XLSX для ДАРС/ХИМИК/ЭРА, GET→JSON для бригад.")
    note("Enter — оставить; - — удалить значение. Токен вводится скрыто. SQL Server хранит данные приложения.")
    for profile_id, profile in selected.items():
        source = str(profile.get("source_id", profile_id)).upper()
        title = profile.get("short_title") or profile.get("title") or source
        if profile.get("adapter") or profile.get("pull") is False:
            if args.source and any(v is not None for v in (args.url, args.token)):
                raise Stop(f"{title}: профиль использует адаптер или только push/файлы. URL не заменяет этот режим; согласуйте профиль с администратором.")
            note(f"{title}: адаптер / push-профиль; настройки сохраняются.")
            continue
        url_key, token_key = source_setting(source, "URL"), source_setting(source, "TOKEN")
        endpoint = _ask(f"{title}: URL (пусто — загрузка файлами)", values.get(url_key, ""), interactive=interactive,
                        preset=args.url, check=lambda v: None if v in {"", "-"} else source_url_problem(v))
        values[url_key] = "" if endpoint == "-" else endpoint
        token = _ask(f"{title}: Bearer-токен", values.get(token_key, ""), interactive=interactive,
                     preset=args.token, secret=True, shown_default="оставить прежний" if values.get(token_key) else "",
                     check=_single_line) if values[url_key] or args.token is not None else values.get(token_key, "")
        values[token_key] = "" if token == "-" else token
    ids = _ask("ID скважин через запятую (для пустого фонда; - — убрать)", values.get("MAI_WELL_IDS", ""),
               interactive=interactive, preset=args.well_ids, check=_single_line)
    values["MAI_WELL_IDS"] = _wells("" if ids == "-" else ids)
    if ENV_FILE.is_file():
        shutil.copy2(ENV_FILE, ENV_FILE.with_name(".env.bak"))
    ENV_FILE.write_text(envfile.render(TEMPLATE, values), encoding="utf-8")
    ok("Настройки источников сохранены; прежний .env — в .env.bak, если файл уже существовал.")
    say("Проверка: mai.cmd sources check --well <ID>. Для применения в работающем API: mai.cmd restart.")
    return 0
