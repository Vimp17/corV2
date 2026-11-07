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
_TOO_LARGE = "Source response exceeds the configured size limit"


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
                       origin: str = "manual_upload", auto_header: bool = False) -> dict:
    """Import a workbook by its source profile; one source status per well that received data. The same
    file imported again replaces what it loaded before (its reading may have changed since)."""
    parsed = parse_excel(content, filename, profile_id, well_id, auto_header=auto_header)
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
        raise ExcelImportError("JSON must contain a 'crew_availability' array.")
    if len(rows) > MAX_CREWS:
        raise ExcelImportError(f"Maximum {MAX_CREWS} crew records per request")
    crews, errors = [], []
    for index, row in enumerate(rows, start=1):
        try:
            if not isinstance(row, dict):
                raise ValueError("Each item must be a JSON object")
            crews.append(CrewAvailability.model_validate({**row, "source_id": source_id}).model_dump(mode="json"))
        except ValueError as exc:  # a pydantic ValidationError is a ValueError
            errors.append({"row": index, "message": str(exc)[:500]})
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
        raise ExcelImportError(f"Crew schedule row {errors[0]['row']} is invalid: {errors[0]['message']}"[:2000])
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


def _store_json(answer: Any, profile_id: str, profile: dict, source_id: str, well_id: str) -> list[dict]:
    """An adapter's answer: workbook bytes, or canonical JSON — telemetry {"records": […]}, history
    {"work_history": […], "failure_history": […]} or a crew schedule. Returns the source statuses."""
    if isinstance(answer, bytes):
        return ingest_excel_bytes(answer, f"{profile_id}.xlsx", profile_id, well_id, origin="adapter")["source_statuses"]
    kind = profile.get("kind")
    if kind == "json_schedule":
        return save_crews(_strict_crews(answer, source_id), source_id, [well_id], origin="adapter")
    if not isinstance(answer, dict):
        raise ExcelImportError("Source adapter must return workbook bytes or a canonical JSON object")
    batch = _batch_id(source_id, well_id, hashlib.sha256(json.dumps(answer, sort_keys=True, default=str).encode()).hexdigest())
    if kind == "telemetry":
        records = answer.get("records")
        if not isinstance(records, list) or not records or not all(isinstance(record, dict) for record in records):
            raise ExcelImportError("Telemetry adapter JSON must contain a non-empty records list of objects")
        allowed = set(TelemetryPoint.model_fields)
        points = [TelemetryPoint.model_validate({**{key: value for key, value in record.items() if key in allowed},
                                                 "source_id": source_id}) for record in records]
        for point in points:
            point.timestamp = in_source_zone(point.timestamp)
        db.save_batch(well_id, batch, points)
        count = len(points)
    elif kind == "history":
        work = [WorkHistoryItem.model_validate(item) for item in answer.get("work_history", [])]
        failures = [FailureHistoryItem.model_validate(item) for item in answer.get("failure_history", [])]
        for item in work:
            item.date = in_source_zone(item.date)
        for item in failures:
            item.failure_date = in_source_zone(item.failure_date)
        if not work and not failures:
            raise ExcelImportError("History adapter returned no work_history or failure_history records")
        db.save_history(well_id, batch, work, failures, source_id=source_id)
        count = len(work) + len(failures)
    else:
        raise ExcelImportError(f"No JSON adapter contract is defined for source kind '{kind}'")
    return [db.record_source_status(source_id, well_id, "ok", None, count, origin="adapter")]


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
def source_env(source_id: str, suffix: str) -> str:
    """MAI_SOURCE_<ID>_<SUFFIX>: the id upper-cased, other characters as single underscores."""
    safe_id = "_".join(part for part in "".join(c if c.isalnum() else "_" for c in source_id.upper()).split("_") if part)
    return os.getenv(f"MAI_SOURCE_{safe_id}_{suffix}", "").strip()


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
        response.raise_for_status()
        if int(response.headers.get("Content-Length") or 0) > settings.max_excel_bytes:
            raise ExcelImportError(_TOO_LARGE)
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > settings.max_excel_bytes:
                raise ExcelImportError(_TOO_LARGE)
            chunks.append(chunk)
        return b"".join(chunks)


def _from_url(well_id: str, profile_id: str, profile: dict, source_id: str) -> list[dict]:
    def status(value: str, message: str | None = None, count: int | None = None) -> list[dict]:
        return [db.record_source_status(source_id, well_id, value, message, count, origin="api_refresh")]

    endpoint = source_env(source_id, "URL")
    if not endpoint:
        return status("not_configured", f"Set MAI_SOURCE_{source_id}_URL to connect this Excel source.", 0)
    address = urlparse(endpoint)
    if address.scheme not in {"http", "https"} or not address.netloc or address.username or address.password:
        return status("error", "Configured source URL is invalid.")
    schedule = profile.get("kind") == "json_schedule"
    headers = {"Accept": "application/json" if schedule
               else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/octet-stream"}
    if token := source_env(source_id, "TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        content = _download(endpoint, well_id, headers)
        if content is None:
            return status("unavailable", "Source API has no workbook for this well (HTTP 404).", 0)
        if not content:
            return status("unavailable", "Source API returned an empty file/JSON response.", 0)
        if schedule:
            return save_crews(_strict_crews(json.loads(content), source_id), source_id, [well_id], origin="api_refresh")
        return ingest_excel_bytes(content, f"{source_id.lower()}.xlsx", profile_id, well_id,
                                  origin="api_refresh")["source_statuses"]
    except ExcelImportError as exc:
        return status("error", str(exc)[:2000])
    except (ValueError, ValidationError) as exc:
        return status("error", f"Source response did not match its profile ({type(exc).__name__}).")
    except requests.RequestException as exc:
        return status("error", f"Source request failed ({type(exc).__name__}).")


# --------------------------------------------------------------------------- per well
def source_modes() -> list[dict]:
    """How each per-well source gets its data: pulled from an API/adapter, or uploads only."""
    modes = []
    for profile_id, profile in load_profiles().items():
        if profile.get("kind") not in {"telemetry", "history"}:
            continue
        source_id = str(profile.get("source_id", profile_id)).upper()
        if profile.get("pull") is False:
            mode = "upload"
        elif profile.get("adapter"):
            mode = "adapter" if profile["adapter"] in _load_adapters() else "adapter_missing"
        else:
            mode = "api" if source_env(source_id, "URL") else "upload"
        modes.append({"profile_id": profile_id, "source_id": source_id,
                      "title": profile.get("short_title") or profile.get("title") or source_id, "mode": mode})
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
