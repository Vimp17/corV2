from __future__ import annotations

import hashlib
import json
import logging
import os
from importlib import metadata
from typing import Any, Protocol
from urllib.parse import urlparse

import requests
from pydantic import ValidationError

from app import db
from app.config import settings
from app.core.excel_ingestion import ExcelImportError, load_profiles, parse_excel
from app.schemas import (
    CrewAvailability,
    FailureHistoryItem,
    TelemetryPoint,
    WorkHistoryItem,
)

logger = logging.getLogger(__name__)
SOURCE_ENTRY_POINT_GROUP = "mai_corrosion.sources"


class SourceAdapter(Protocol):
    def fetch_for_well(self, well_id: str, profile: dict[str, Any]) -> bytes | dict[str, Any]:
        """Return an .xlsx workbook or canonical JSON for the configured profile."""
        ...


def _batch_id(source_id: str, well_id: str, digest: str) -> str:
    well_key = hashlib.sha256(well_id.encode("utf-8")).hexdigest()[:16]
    return f"{source_id[:24]}-{well_key}-{digest[:48]}"


def ingest_excel_bytes(
    content: bytes,
    filename: str,
    profile_id: str,
    well_id: str | None = None,
    origin: str = "manual_upload",
) -> dict:
    parsed = parse_excel(content, filename, profile_id, well_id)
    source_id = parsed["source_id"]

    imported: list[dict] = []
    source_statuses: list[dict] = []

    for target_well, group in parsed["groups"].items():
        if parsed["kind"] == "telemetry":
            points = group["records"]
            if not points:
                continue

            batch_id = _batch_id(source_id, target_well, parsed["file_sha256"])
            stored = db.save_batch(target_well, batch_id, points)
            record_count = len(points)
        else:
            work, failures = group["work_history"], group["failure_history"]
            if not work and not failures:
                continue

            batch_id = _batch_id(source_id, target_well, parsed["file_sha256"])
            stored = db.save_history(
                target_well,
                batch_id,
                work,
                failures,
                source_id=source_id,
            )
            record_count = len(work) + len(failures)

        status_row = db.record_source_status(
            source_id,
            target_well,
            parsed["status"],
            _status_message(parsed),
            record_count,
            origin=origin,
        )
        source_statuses.append(status_row)

        imported.append({
            "well_id": target_well,
            "source_id": source_id,
            "status": parsed["status"],
            "record_count": record_count,
            **stored,
        })

    if not imported:
        status = "unavailable" if not parsed["rows_accepted"] else "error"
        message = (
            "Workbook contained no usable rows for import. "
            "Check the sheet, required columns and row diagnostics."
        )
        target_wells = [well_id] if well_id else []

        for target_well in target_wells:
            status_row = db.record_source_status(
                source_id,
                target_well,
                status,
                message,
                0,
                origin=origin,
            )
            source_statuses.append(status_row)

    return {
        "status": parsed["status"] if imported else "unavailable",
        "source_id": source_id,
        "profile_id": profile_id,
        "kind": parsed["kind"],
        "sheet_name": parsed["sheet_name"],
        "rows_read": parsed["rows_read"],
        "rows_accepted": parsed["rows_accepted"],
        "rows_rejected": parsed["rows_rejected"],
        "source_columns": parsed["source_columns"],
        "ignored_columns": parsed["ignored_columns"],
        "errors": parsed["errors"],
        "imported_wells": imported,
        "source_statuses": source_statuses,
        "origin": origin,
        "message": None if imported and parsed["status"] == "ok" else _status_message(parsed),
    }


def _status_message(parsed: dict) -> str:
    summary = (
        f"{parsed['rows_accepted']} rows accepted, {parsed['rows_rejected']} rejected "
        f"out of {parsed['rows_read']} read. "
    )
    if parsed["errors"]:
        summary += " Examples: " + "; ".join(
            f"row {item['row']}: {item['message']}" for item in parsed["errors"][:5]
        )
    return summary[:2000]


def source_env(source_id: str, suffix: str) -> str:
    safe_id = "".join(
        char if char.isalnum() else "_"
        for char in source_id.upper()
    )
    safe_id = "_".join(part for part in safe_id.split("_") if part)
    return os.getenv(f"MAI_SOURCE_{safe_id}_{suffix}", "").strip()


def _load_adapters() -> dict[str, SourceAdapter]:
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


def _ingest_adapter_response(
    source_id: str,
    profile_id: str,
    profile: dict,
    well_id: str,
    response,
) -> dict:
    if isinstance(response, bytes):
        return ingest_excel_bytes(
            response,
            f"{profile_id}.xlsx",
            profile_id,
            well_id,
            origin="adapter",
        )

    if not isinstance(response, dict):
        raise ExcelImportError("Source adapter must return workbook bytes or a canonical JSON object")

    kind = profile.get("kind")

    if kind == "json_schedule":
        rows = response if isinstance(response, list) else response.get("crew_availability")
        if not isinstance(rows, list):
            raise ExcelImportError("Schedule adapter must return a JSON list or crew_availability")

        normalized = [
            CrewAvailability.model_validate({**item, "source_id": source_id}).model_dump(mode="json")
            for item in rows[:5000]
        ]
        db.save_source_payload(source_id, well_id, {"crew_availability": normalized})

        status_row = db.record_source_status(
            source_id,
            well_id,
            "ok",
            None,
            len(normalized),
            origin="adapter",
        )

        return {
            "status": "ok",
            "rows_accepted": len(normalized),
            "rows_rejected": 0,
            "imported_wells": [{"well_id": well_id}],
            "source_statuses": [status_row],
            "message": None,
        }

    if kind == "telemetry":
        records = response.get("records")
        if not isinstance(records, list) or not records:
            raise ExcelImportError("Telemetry adapter JSON must contain a non-empty records list")

        canonical = []
        allowed = set(TelemetryPoint.model_fields) | {"source_id"}
        for record in records:
            if not isinstance(record, dict):
                raise ExcelImportError("Each telemetry adapter record must be an object")
            canonical.append(TelemetryPoint.model_validate({
                **{key: value for key, value in record.items() if key in allowed},
                "source_id": source_id,
            }))

        digest = hashlib.sha256(json.dumps(response, sort_keys=True, default=str).encode()).hexdigest()
        stored = db.save_batch(well_id, _batch_id(source_id, well_id, digest), canonical)

        status_row = db.record_source_status(
            source_id,
            well_id,
            "ok",
            None,
            len(canonical),
            origin="adapter",
        )

        return {
            "status": "ok",
            "rows_accepted": len(canonical),
            "rows_rejected": 0,
            "imported_wells": [{"well_id": well_id, **stored}],
            "source_statuses": [status_row],
            "message": None,
        }

    if kind == "history":
        work = [WorkHistoryItem.model_validate(item) for item in response.get("work_history", [])]
        failures = [FailureHistoryItem.model_validate(item) for item in response.get("failure_history", [])]

        if not work and not failures:
            raise ExcelImportError("History adapter returned no work_history or failure_history records")

        digest = hashlib.sha256(json.dumps(response, sort_keys=True, default=str).encode()).hexdigest()
        stored = db.save_history(well_id, _batch_id(source_id, well_id, digest), work, failures, source_id=source_id)

        status_row = db.record_source_status(
            source_id,
            well_id,
            "ok",
            None,
            len(work) + len(failures),
            origin="adapter",
        )

        return {
            "status": "ok",
            "rows_accepted": len(work) + len(failures),
            "rows_rejected": 0,
            "imported_wells": [{"well_id": well_id, **stored}],
            "source_statuses": [status_row],
            "message": None,
        }

    raise ExcelImportError(f"No JSON adapter contract is defined for source kind '{kind}'")


def refresh_sources_for_well(well_id: str) -> list[dict]:
    """Fetch each configured per-well source using its Excel, JSON, or plugin contract."""
    statuses: list[dict] = []
    adapters = _load_adapters()

    for profile_id, profile in load_profiles().items():
        source_id = str(profile.get("source_id", profile_id)).upper()
        adapter_name = profile.get("adapter")

        if adapter_name:
            adapter = adapters.get(adapter_name)

            if adapter is None:
                statuses.append(
                    db.record_source_status(
                        source_id,
                        well_id,
                        "not_configured",
                        f"Source adapter '{adapter_name}' is not installed. Register it in {SOURCE_ENTRY_POINT_GROUP}.",
                        0,
                        origin="adapter",
                    )
                )
                continue

            try:
                response = adapter.fetch_for_well(well_id, profile)
                result = _ingest_adapter_response(
                    source_id,
                    profile_id,
                    profile,
                    well_id,
                    response,
                )
                statuses.extend(result.get("source_statuses") or [])
            except Exception as exc:
                logger.warning(
                    "Source adapter failed source=%s well_id=%s type=%s",
                    source_id,
                    well_id,
                    type(exc).__name__,
                )
                statuses.append(
                    db.record_source_status(
                        source_id,
                        well_id,
                        "error",
                        f"Source adapter failed ({type(exc).__name__}).",
                        None,
                        origin="adapter",
                    )
                )
            continue

        endpoint = source_env(source_id, "URL")

        if not endpoint:
            statuses.append(
                db.record_source_status(
                    source_id,
                    well_id,
                    "not_configured",
                    f"Set MAI_SOURCE_{source_id}_URL to connect this Excel source.",
                    0,
                    origin="api_refresh",
                )
            )
            continue

        parsed_url = urlparse(endpoint)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.username
            or parsed_url.password
        ):
            statuses.append(
                db.record_source_status(
                    source_id,
                    well_id,
                    "error",
                    "Configured source URL is invalid.",
                    None,
                    origin="api_refresh",
                )
            )
            continue

        token = source_env(source_id, "TOKEN")
        is_schedule = profile.get("kind") == "json_schedule"

        headers = {
            "Accept": (
                "application/json"
                if is_schedule
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/octet-stream"
            )
        }

        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            response = requests.get(
                endpoint,
                params={"well_id": well_id},
                headers=headers,
                timeout=(5, 60),
                allow_redirects=False,
                stream=True,
            )

            try:
                if response.status_code == 404:
                    statuses.append(
                        db.record_source_status(
                            source_id,
                            well_id,
                            "unavailable",
                            "Source API has no workbook for this well (HTTP 404).",
                            0,
                            origin="api_refresh",
                        )
                    )
                    continue

                response.raise_for_status()

                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > settings.max_excel_bytes:
                    raise ExcelImportError("Source response exceeds the configured size limit")

                chunks: list[bytes] = []
                total_bytes = 0

                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue

                    total_bytes += len(chunk)

                    if total_bytes > settings.max_excel_bytes:
                        raise ExcelImportError("Source response exceeds the configured size limit")

                    chunks.append(chunk)

                content = b"".join(chunks)
            finally:
                response.close()

            if not content:
                statuses.append(
                    db.record_source_status(
                        source_id,
                        well_id,
                        "unavailable",
                        "Source API returned an empty file/JSON response.",
                        0,
                        origin="api_refresh",
                    )
                )
                continue

            if is_schedule:
                data = json.loads(content)

                crew_rows = (
                    data
                    if isinstance(data, list)
                    else data.get("crew_availability")
                    if isinstance(data, dict)
                    else None
                )

                if not isinstance(crew_rows, list):
                    raise ExcelImportError(
                        "Crew schedule response must be a JSON list or contain crew_availability"
                    )

                canonical_rows = []
                for row in crew_rows[:5000]:
                    if not isinstance(row, dict):
                        raise ExcelImportError("Each crew schedule item must be a JSON object")

                    canonical_rows.append(
                        CrewAvailability.model_validate(
                            {**row, "source_id": source_id}
                        ).model_dump(mode="json")
                    )

                payload = {"crew_availability": canonical_rows}
                db.save_source_payload(source_id, well_id, payload)

                statuses.append(
                    db.record_source_status(
                        source_id,
                        well_id,
                        "ok",
                        None,
                        len(canonical_rows),
                        origin="api_refresh",
                    )
                )
                continue

            result = ingest_excel_bytes(
                content,
                f"{source_id.lower()}.xlsx",
                profile_id,
                well_id,
                origin="api_refresh",
            )

            statuses.extend(result.get("source_statuses") or [])

        except ExcelImportError as exc:
            statuses.append(
                db.record_source_status(
                    source_id,
                    well_id,
                    "error",
                    str(exc)[:2000],
                    None,
                    origin="api_refresh",
                )
            )

        except (ValueError, ValidationError) as exc:
            statuses.append(
                db.record_source_status(
                    source_id,
                    well_id,
                    "error",
                    f"Source response did not match its profile ({type(exc).__name__}).",
                    None,
                    origin="api_refresh",
                )
            )

        except requests.RequestException as exc:
            statuses.append(
                db.record_source_status(
                    source_id,
                    well_id,
                    "error",
                    f"Source request failed ({type(exc).__name__}).",
                    None,
                    origin="api_refresh",
                )
            )

    return statuses