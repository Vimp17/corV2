"""Source data by API: a workbook or JSON pulled for each well from MAI_SOURCE_<ID>_URL, fetched by an
adapter package (entry point group mai_corrosion.sources), or uploaded / pushed (app.api.routes.ingestion).
Whatever brings the data, it is stored the same way and leaves a source status (db.source_health)."""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import time
from importlib import metadata
from typing import Any, Protocol
from urllib.parse import urlparse

import requests
from pydantic import ValidationError

from app import db
from app.config import settings
from app.core.excel_ingestion import ExcelImportError, load_profiles, parse_excel
from app.schemas import CrewAvailability, FailureHistoryItem, TelemetryPoint, WorkHistoryItem, in_source_zone

logger = logging.getLogger(__name__)
SOURCE_ENTRY_POINT_GROUP = "mai_corrosion.sources"
MAX_CREWS = 5000
_RETRYABLE_STATUS = {429, 502, 503, 504}
_TOO_LARGE = "Ответ источника превышает допустимый размер файла. Уменьшите период выгрузки."


class SourceAdapter(Protocol):
    def fetch_for_well(self, well_id: str, profile: dict[str, Any]) -> bytes | dict[str, Any]:
        """Return an .xlsx workbook or canonical JSON for the configured profile."""
        ...


def _batch_id(source_id: str, well_id: str, digest: str) -> str:
    well_key = hashlib.sha256(well_id.encode("utf-8")).hexdigest()[:16]
    return f"{source_id[:24]}-{well_key}-{digest[:48]}"


def _status_message(parsed: dict) -> str:
    summary = f"{parsed['rows_accepted']} rows accepted, {parsed['rows_rejected']} rejected out of {parsed['rows_read']} read. "
    if parsed["errors"]:
        summary += " Examples: " + "; ".join(f"row {item['row']}: {item['message']}" for item in parsed["errors"][:5])
    if parsed.get("skipped_cell_count"):
        summary += f" Non-numeric cells left out: {parsed['skipped_cell_count']}."
    return summary[:2000]


def ingest_excel_bytes(content: bytes, filename: str, profile_id: str, well_id: str | None = None,
                       origin: str = "manual_upload", auto_header: bool = False, require_well_match: bool = False) -> dict:
    """Import a workbook by its source profile; one source status per well that received data. The same
    file imported again replaces what it loaded before (its reading may have changed since)."""
    parsed = parse_excel(content, filename, profile_id, well_id, auto_header=auto_header, require_well_match=require_well_match)
    source_id = parsed["source_id"]
    imported: list[dict] = []
    statuses: list[dict] = []
    for target, group in parsed["groups"].items():
        batch = _batch_id(source_id, target, parsed["file_sha256"])
        if parsed["kind"] == "telemetry":
            if not group["records"]:
                continue
            stored, count = db.save_batch(target, batch, group["records"], replace=True), len(group["records"])
        else:
            work, failures = group["work_history"], group["failure_history"]
            if not work and not failures:
                continue
            stored = db.save_history(target, batch, work, failures, source_id=source_id, replace=True)
            count = len(work) + len(failures)
        statuses.append(db.record_source_status(source_id, target, parsed["status"], _status_message(parsed), count,
                                                origin=origin))
        imported.append({"well_id": target, "source_id": source_id, "status": parsed["status"], "record_count": count,
                         **stored})
    if not imported and well_id:
        statuses.append(db.record_source_status(
            source_id, well_id, "error" if parsed["rows_accepted"] else "unavailable",
            "Workbook contained no usable rows for import. Check the sheet, required columns and row diagnostics.",
            0, origin=origin))
    return {"status": parsed["status"] if imported else "unavailable", "source_id": source_id, "profile_id": profile_id,
            **{key: parsed[key] for key in ("kind", "sheet_name", "sheet_names", "skipped_sheets", "rows_read",
                                            "rows_accepted", "rows_rejected", "source_columns", "ignored_columns",
                                            "extra_columns", "errors", "skipped_cell_count", "skipped_cells")},
            "imported_wells": imported, "source_statuses": statuses, "origin": origin,
            "message": None if imported and parsed["status"] == "ok" and not parsed["skipped_cell_count"]
            else _status_message(parsed)}


# --------------------------------------------------------------------------- crew schedule
def crew_rows(data: Any, source_id: str) -> tuple[list[dict], list[dict]]:
    """Valid crew availability rows of a JSON list or an object with "crew_availability" (or "records"),
    and the errors of the other rows. ExcelImportError without such a list or with too many rows."""
    rows = data if isinstance(data, list) else next(
        (data[key] for key in ("crew_availability", "records") if isinstance(data, dict) and isinstance(data.get(key), list)),
        None)
    if rows is None:
        raise ExcelImportError("В JSON требуется массив crew_availability (или records) расписания бригад.")
    if len(rows) > MAX_CREWS:
        raise ExcelImportError(f"В расписании допускается не более {MAX_CREWS} записей.")
    crews, errors = [], []
    for index, row in enumerate(rows, start=1):
        try:
            if not isinstance(row, dict):
                raise ValueError("Каждая запись расписания должна быть объектом JSON.")
            crews.append(CrewAvailability.model_validate({**row, "source_id": source_id}).model_dump(mode="json"))
        except ValueError as exc:  # a pydantic ValidationError is a ValueError
            errors.append({"row": index, "message": source_error(exc) if isinstance(exc, ValidationError) else str(exc)})
    return crews, errors


def save_crews(crews: list[dict], source_id: str, wells: list[str], origin: str, status: str = "ok",
               message: str | None = None) -> list[dict]:
    """Store the schedule for each well; returns the source statuses."""
    statuses = []
    for well in wells:
        db.save_source_payload(source_id, well, {"crew_availability": crews})
        statuses.append(db.record_source_status(source_id, well, status, message, len(crews), origin=origin))
    return statuses


def _strict_crews(data: Any, source_id: str) -> list[dict]:
    """A pulled or fetched schedule is taken whole or not at all."""
    crews, errors = crew_rows(data, source_id)
    if errors:
        raise ExcelImportError(f"Расписание, строка {errors[0]['row']}: {errors[0]['message']}"[:2000])
    return crews


# --------------------------------------------------------------------------- adapters
@functools.lru_cache(maxsize=1)
def _load_adapters() -> dict[str, SourceAdapter]:
    """Adapter packages, enumerated once: entry points are fixed for the life of the process."""
    adapters: dict[str, SourceAdapter] = {}
    try:
        points = metadata.entry_points(group=SOURCE_ENTRY_POINT_GROUP)
    except Exception as exc:
        logger.warning("Could not enumerate source adapters (%s)", type(exc).__name__)
        return adapters
    for point in points:
        try:
            loaded = point.load()
            adapter = loaded() if isinstance(loaded, type) else loaded
            if not callable(getattr(adapter, "fetch_for_well", None)):
                raise TypeError("source adapter must provide fetch_for_well(well_id, profile)")
            adapters[point.name] = adapter
        except Exception as exc:
            logger.warning("Source adapter %s could not be loaded (%s)", point.name, type(exc).__name__)
    return adapters


def _prepare_json(answer: Any, profile: dict, source_id: str) -> dict:
    """Validate an adapter payload once; both importing and dry-run checks use it."""
    kind = profile.get("kind")
    if kind == "json_schedule":
        return {"crews": _strict_crews(answer, source_id)}
    if not isinstance(answer, dict):
        raise ExcelImportError("Адаптер должен вернуть XLSX или объект JSON согласованного формата.")
    if kind == "telemetry":
        records = answer.get("records")
        if not isinstance(records, list) or not records or not all(isinstance(record, dict) for record in records):
            raise ExcelImportError("В JSON адаптера требуется непустой список records.")
        allowed = set(TelemetryPoint.model_fields)
        points = [TelemetryPoint.model_validate({**{key: value for key, value in record.items() if key in allowed},
                                                 "source_id": source_id}) for record in records]
        for point in points:
            point.timestamp = in_source_zone(point.timestamp)
        return {"records": points}
    if kind == "history":
        work = [WorkHistoryItem.model_validate(item) for item in answer.get("work_history", [])]
        failures = [FailureHistoryItem.model_validate(item) for item in answer.get("failure_history", [])]
        for item in work:
            item.date = in_source_zone(item.date)
        for item in failures:
            item.failure_date = in_source_zone(item.failure_date)
        if not work and not failures:
            raise ExcelImportError("В JSON адаптера отсутствуют события ремонта/отказа.")
        return {"work_history": work, "failure_history": failures}
    raise ExcelImportError("Неизвестный тип данных адаптера.")


def _store_json(answer: Any, profile_id: str, profile: dict, source_id: str, well_id: str) -> list[dict]:
    if isinstance(answer, bytes):
        return ingest_excel_bytes(answer, f"{profile_id}.xlsx", profile_id, well_id, origin="adapter", require_well_match=True)["source_statuses"]
    prepared = _prepare_json(answer, profile, source_id)
    if "crews" in prepared:
        return save_crews(prepared["crews"], source_id, [well_id], origin="adapter")
    batch = _batch_id(source_id, well_id, hashlib.sha256(json.dumps(answer, sort_keys=True, default=str).encode()).hexdigest())
    if "records" in prepared:
        db.save_batch(well_id, batch, prepared["records"])
    else:
        db.save_history(well_id, batch, prepared["work_history"], prepared["failure_history"], source_id=source_id)
    return [db.record_source_status(source_id, well_id, "ok", None, sum(map(len, prepared.values())), origin="adapter")]


def _from_adapter(well_id: str, profile_id: str, profile: dict, source_id: str) -> list[dict]:
    name = profile["adapter"]
    adapter = _load_adapters().get(name)
    if adapter is None:
        return [db.record_source_status(source_id, well_id, "not_configured", f"Source adapter '{name}' is not "
                                        f"installed. Register it in {SOURCE_ENTRY_POINT_GROUP}.", 0, origin="adapter")]
    try:
        return _store_json(adapter.fetch_for_well(well_id, profile), profile_id, profile, source_id, well_id)
    except Exception as exc:
        logger.warning("Source adapter failed source=%s well_id=%s type=%s", source_id, well_id, type(exc).__name__)
        return [db.record_source_status(source_id, well_id, "error", f"Source adapter failed ({type(exc).__name__}).",
                                        None, origin="adapter")]


# --------------------------------------------------------------------------- pull by URL
def source_setting(source_id: str, suffix: str) -> str:
    """MAI_SOURCE_<ID>_<SUFFIX>: the id upper-cased, other characters as single underscores."""
    safe_id = "_".join(part for part in "".join(c if c.isalnum() else "_" for c in source_id.upper()).split("_") if part)
    return f"MAI_SOURCE_{safe_id}_{suffix}"


def source_env(source_id: str, suffix: str) -> str:
    return os.getenv(source_setting(source_id, suffix), "").strip()


def _get_with_retries(endpoint: str, **kwargs) -> requests.Response:
    """GET with bounded retries for transient failures (connection errors, timeouts and
    HTTP 429/502/503/504), backing off 1 s, 2 s, 4 s ... between attempts."""
    attempts = settings.source_max_retries + 1
    for attempt in range(attempts):
        last_try = attempt == attempts - 1
        try:
            response = requests.get(endpoint, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if last_try:
                raise
        else:
            if response.status_code not in _RETRYABLE_STATUS or last_try:
                return response
            response.close()
        time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("unreachable")


def _download(endpoint: str, well_id: str, headers: dict) -> bytes | None:
    """The answer of GET <endpoint>?well_id=…, at most MAI_MAX_EXCEL_BYTES; None for HTTP 404."""
    with _get_with_retries(endpoint, params={"well_id": well_id}, headers=headers, timeout=(5, 60),
                           allow_redirects=False, stream=True) as response:
        if response.status_code == 404:
            return None
        if 300 <= response.status_code < 400:
            raise ExcelImportError(f"HTTP {response.status_code}: адрес перенаправляет на другую страницу. Укажите прямой URL выгрузки; редиректы не выполняются.")
        response.raise_for_status()
        if int(response.headers.get("Content-Length") or 0) > settings.max_excel_bytes:
            raise ExcelImportError(_TOO_LARGE)
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > settings.max_excel_bytes:
                raise ExcelImportError(_TOO_LARGE)
            chunks.append(chunk)
        content = b"".join(chunks)
        if content.lstrip().lower().startswith((b"<!doctype html", b"<html")) or "text/html" in response.headers.get("Content-Type", ""):
            raise ExcelImportError("Источник вернул HTML-страницу, возможно вход в систему. Ожидается XLSX или согласованный JSON расписания; проверьте прямой URL и авторизацию.")
        return content


def _from_url(well_id: str, profile_id: str, profile: dict, source_id: str) -> list[dict]:
    def status(value: str, message: str | None = None, count: int | None = None) -> list[dict]:
        return [db.record_source_status(source_id, well_id, value, message, count, origin="api_refresh")]

    endpoint = source_env(source_id, "URL")
    if not endpoint:
        return status("not_configured", f"Автоматический источник не настроен. Доступна загрузка файлов; подключение — mai.cmd sources configure ({source_setting(source_id, 'URL')}).", 0)
    if problem := source_url_problem(endpoint):
        return status("error", problem)
    schedule = profile.get("kind") == "json_schedule"
    try:
        content = _download(endpoint, well_id, _source_headers(profile, source_id))
        if content is None:
            return status("unavailable", "HTTP 404: выгрузка для этой скважины не найдена. Проверьте ID и URL источника.", 0)
        if not content:
            return status("unavailable", "Источник вернул пустой ответ. Проверьте ID скважины и период выгрузки.", 0)
        if schedule:
            return save_crews(_strict_crews(json.loads(content), source_id), source_id, [well_id], origin="api_refresh")
        _expect_workbook(content)
        return ingest_excel_bytes(content, f"{source_id.lower()}.xlsx", profile_id, well_id,
                                  origin="api_refresh", require_well_match=True)["source_statuses"]
    except ExcelImportError as exc:
        return status("error", str(exc)[:2000])
    except (ValueError, ValidationError, requests.RequestException) as exc:
        return status("error", source_error(exc))


def source_url_problem(endpoint: str) -> str | None:
    try:
        address = urlparse(endpoint)
        valid = (address.scheme in {"http", "https"} and address.hostname and not address.username
                 and not address.password and (address.port is None or 1 <= address.port <= 65535)
                 and not any(c.isspace() or c in "{}" for c in endpoint) and not address.fragment)
    except ValueError:
        valid = False
    if not valid:
        return "Некорректный URL: нужен прямой HTTP(S)-адрес без логина/пароля, пробелов и шаблонов. ID добавляется как параметр well_id."
    return None


def source_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        fields = ", ".join(".".join(map(str, error["loc"])) or "период доступности"
                           for error in exc.errors(include_input=False, include_url=False)[:5])
        return f"Проверьте поля JSON: {fields}. Неизвестные поля не принимаются; даты — в формате ISO 8601."
    if isinstance(exc, requests.exceptions.SSLError):
        return "Не удалось проверить TLS-сертификат источника. Проверьте сертификат и доверенный корпоративный центр сертификации."
    if isinstance(exc, requests.Timeout):
        return "Источник не ответил вовремя. Проверьте доступность сервера и повторите проверку."
    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code if exc.response is not None else "?"
        hint = "Проверьте токен и права доступа." if code in {401, 403} else "Проверьте URL и доступность источника."
        return f"HTTP {code}. {hint}"
    if isinstance(exc, requests.ConnectionError):
        return "Нет соединения с источником. Проверьте адрес, сеть и настройки прокси."
    return "Ответ не соответствует профилю источника. Проверьте формат JSON, обязательные поля и имена колонок."


def _expect_workbook(content: bytes) -> None:
    if content.lstrip().startswith((b"{", b"[")):
        raise ExcelImportError("Источник вернул JSON, а этот профиль ожидает XLSX. Нужна XLSX-выгрузка либо адаптер согласованного JSON-формата.")


def _source_headers(profile: dict, source_id: str) -> dict:
    headers = {"Accept": "application/json" if profile.get("kind") == "json_schedule"
               else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/octet-stream"}
    if token := source_env(source_id, "TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def check_source(profile_id: str, well_id: str) -> dict:
    """Fetch and validate one well without storing payloads, statuses or ratings."""
    profiles = load_profiles()
    if profile_id not in profiles:
        raise LookupError("Источник не найден.")
    profile = profiles[profile_id]
    source_id = str(profile.get("source_id", profile_id)).upper()
    report = {"profile_id": profile_id, "source_id": source_id, "record_count": 0, "source_columns": {}}
    try:
        if profile.get("adapter"):
            adapter = _load_adapters().get(profile["adapter"])
            if adapter is None:
                raise ExcelImportError("Адаптер источника не установлен. Установите согласованный адаптер в сборку.")
            answer = adapter.fetch_for_well(well_id, profile)
        else:
            endpoint = source_env(source_id, "URL")
            if not endpoint:
                raise ExcelImportError("Автоматический источник не настроен: используется загрузка файлов. Адрес задаётся через mai.cmd sources configure.")
            if problem := source_url_problem(endpoint):
                raise ExcelImportError(problem)
            answer = _download(endpoint, well_id, _source_headers(profile, source_id))
            if answer is None:
                raise ExcelImportError("HTTP 404: выгрузка для этой скважины не найдена. Проверьте ID и адрес источника.")
            if not answer:
                raise ExcelImportError("Источник вернул пустой ответ: проверьте ID скважины и период выгрузки.")
            if profile.get("kind") == "json_schedule":
                answer = json.loads(answer)
        if isinstance(answer, bytes):
            _expect_workbook(answer)
            parsed = parse_excel(answer, f"{profile_id}.xlsx", profile_id, well_id, require_well_match=True)
            report.update(record_count=sum(len(g.get(k) or []) for g in parsed["groups"].values()
                                           for k in ("records", "work_history", "failure_history")),
                          source_columns=parsed["source_columns"], sheet_name=parsed["sheet_name"],
                          rows_rejected=parsed["rows_rejected"], skipped_cells=parsed.get("skipped_cell_count", 0))
            if not report["record_count"]:
                raise ExcelImportError("Файл прочитан, но подходящих записей нет. Проверьте лист, колонки и ID скважины.")
        else:
            prepared = _prepare_json(answer, profile, source_id)
            report["record_count"] = sum(map(len, prepared.values()))
        warnings = report.get("rows_rejected", 0) or report.get("skipped_cells", 0)
        report.update(status="warning" if warnings else "ok",
                      message="Ответ прочитан и проверен. Данные не импортированы." + (" Есть отклонённые строки/ячейки; исправьте файл перед загрузкой." if warnings else ""))
    except Exception as exc:
        logger.warning("Source check failed source=%s type=%s", source_id, type(exc).__name__)
        message = str(exc) if isinstance(exc, ExcelImportError) else source_error(exc)
        if token := source_env(source_id, "TOKEN"):
            message = message.replace(token, "[скрыто]")
        report.update(status="error", message=message[:2000])
    return report


# --------------------------------------------------------------------------- per well
def source_modes() -> list[dict]:
    """How each per-well source gets its data: pulled from an API/adapter, or uploads only."""
    modes = []
    for profile_id, profile in load_profiles().items():
        source_id = str(profile.get("source_id", profile_id)).upper()
        if profile.get("pull") is False:
            mode = "upload"
        elif profile.get("adapter"):
            mode = "adapter" if profile["adapter"] in _load_adapters() else "adapter_missing"
        else:
            mode = "api" if source_env(source_id, "URL") else "upload"
        modes.append({"profile_id": profile_id, "source_id": source_id,
                      "title": profile.get("short_title") or profile.get("title") or source_id, "kind": profile["kind"], "mode": mode})
    return modes


def refresh_sources_for_well(well_id: str) -> list[dict]:
    """Fetch each per-well source of the profiles — by its adapter, else from its URL — and store it.
    Profiles marked "pull": false are uploaded or pushed only."""
    statuses: list[dict] = []
    for profile_id, profile in load_profiles().items():
        if profile.get("pull") is False:
            continue
        source_id = str(profile.get("source_id", profile_id)).upper()
        fetch = _from_adapter if profile.get("adapter") else _from_url
        statuses += fetch(well_id, profile_id, profile, source_id)
    return statuses
