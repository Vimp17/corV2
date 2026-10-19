"""Processing one document end to end, the queue, and the background worker.

Statuses: queued -> processing -> loaded | not_recognized | failed.
  loaded          - at least one record passed the checks and was written to the data;
  not_recognized  - no text, unknown type, or nothing could be confirmed: nothing is loaded and
                    the well's data counts as missing (e.g. "нет акта" stays in the rating);
  failed          - the file is damaged, or OCR / the LLM is not configured or stayed
                    unavailable after MAI_DOCS_MAX_ATTEMPTS tries (MAI_DOCS_RETRY_MINUTES apart).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app import db
from app.config import settings
from app.core.documents import extract, intake, load, text as doc_text
from app.core.documents.types import DocumentTypesError, load_document_types

logger = logging.getLogger(__name__)

MAX_STORED_TEXT = 200_000
MIN_TEXT_CHARS = 30
STATUS_LABELS = {
    "queued": "В очереди",
    "processing": "Обрабатывается",
    "loaded": "Загружен",
    "not_recognized": "Не распознан",
    "failed": "Ошибка",
}


def type_titles() -> dict[str, str]:
    try:
        return {type_id: spec.get("title") or type_id for type_id, spec in load_document_types().items()}
    except DocumentTypesError:
        return {}


def decorate(row: dict, titles: dict[str, str] | None = None) -> dict:
    """Status and type in words for the dashboard."""
    titles = type_titles() if titles is None else titles
    doc_type = row.get("doc_type")
    if doc_type and doc_type.startswith("source:"):
        type_title = f"Выгрузка {doc_type.split(':', 1)[1].upper()}"
    else:
        type_title = titles.get(doc_type or "", "")
    return {**row, "status_label": STATUS_LABELS.get(row.get("status"), row.get("status")), "type_title": type_title}


def llm_configured() -> bool:
    return bool(settings.llm_api_url and settings.extraction_model)


def _finish(document_id: str, status: str, message: str | None, **values) -> dict:
    db.update_document(document_id, status=status, status_message=(message or "")[:2000] or None,
                       processed_at=datetime.now(timezone.utc), **values)
    return db.get_document(document_id) or {}


def _recompute(wells: list[str], app_state) -> None:
    if not wells or app_state is None:
        return
    from app.core import rating_builder

    rating_builder.recompute_wells(wells, app_state)


def _import_by_profile(document: dict, content: bytes, profile_id: str, app_state) -> dict:
    """An Excel table in the ДАРС / ХИМИК / ЭРА layout goes through the usual source import."""
    from app.core.source_connectors import ingest_excel_bytes

    filename = document["filename"] if document["filename"].casefold().endswith(".xlsx") else "document.xlsx"
    result = ingest_excel_bytes(content, filename, profile_id, origin="document")
    imported = result.get("imported_wells") or []
    wells = sorted({item["well_id"] for item in imported})
    loaded = {"import_profile": profile_id, "source_id": result.get("source_id"), "wells": wells,
              "records": sum(item.get("record_count") or 0 for item in imported),
              "batch_ids": [item["batch_id"] for item in imported if item.get("batch_id")],
              "rows_rejected": result.get("rows_rejected")}
    if not imported:
        return _finish(document["document_id"], "not_recognized",
                       "Таблица похожа на выгрузку источника, но строк для загрузки нет.",
                       doc_type=f"source:{profile_id}", text_method="excel_profile", loaded=loaded, well_ids=[])
    _recompute(wells, app_state)
    rejected = result.get("rows_rejected") or 0
    message = f"Импорт как {result.get('source_id')}: {loaded['records']} записей по {len(wells)} скв."
    if rejected:
        message += f" Отклонено строк: {rejected}."
    return _finish(document["document_id"], "loaded", message, doc_type=f"source:{profile_id}",
                   text_method="excel_profile", loaded=loaded, well_ids=wells)


def _retry_or_fail(document: dict, reason: str, configured: bool) -> dict:
    if configured and (document.get("attempts") or 0) < settings.docs_max_attempts:
        db.update_document(document["document_id"], status="queued",
                           status_message=f"Повтор через {settings.docs_retry_minutes} мин: {reason}"[:2000])
        return db.get_document(document["document_id"]) or {}
    return _finish(document["document_id"], "failed", reason)


def process_document(document_id: str, app_state=None) -> dict:
    """Run one claimed document through text -> type -> fields -> checks -> data."""
    document = db.get_document(document_id)
    if document is None:
        return {}
    try:
        content = intake.read_original(document)
    except OSError:
        return _finish(document_id, "failed", "Оригинал файла не найден в хранилище.")

    if document["file_kind"] == "xlsx":
        try:
            profile_id = doc_text.detect_profile(content, document["filename"])
        except Exception:
            logger.exception("Profile detection failed document_id=%s", document_id)
            profile_id = None
        if profile_id:
            return _import_by_profile(document, content, profile_id, app_state)

    try:
        result = doc_text.document_text(document, content)
    except doc_text.TextUnavailable as exc:
        return _retry_or_fail(document, str(exc), doc_text.ocr_configured())
    except ValueError as exc:
        return _finish(document_id, "failed", str(exc))
    text = result.text[:MAX_STORED_TEXT]
    text_values = {"text": text, "text_method": result.method, "text_chars": len(result.text),
                   "pages": result.pages}
    db.delete_document_data(document_id)
    if len("".join(text.split())) < MIN_TEXT_CHARS:
        return _finish(document_id, "not_recognized", "В документе не найден читаемый текст.",
                       well_ids=[], loaded=None, extraction=None, doc_type=None, **text_values)

    try:
        types = load_document_types()
    except DocumentTypesError as exc:
        return _finish(document_id, "failed", str(exc), **text_values)
    try:
        type_id = extract.classify_by_keywords(text, types)
        type_method = "keywords"
        if type_id is None:
            type_id = extract.classify_by_llm(text, types)
            type_method = "llm"
        if type_id is None:
            return _finish(document_id, "not_recognized",
                           "Тип документа не определён: это не акт демонтажа, акт разбора, протокол "
                           "хим. анализа или паспорт скважины.",
                           well_ids=[], loaded=None, extraction={"type_method": type_method},
                           doc_type=None, **text_values)
        spec = types[type_id]
        raw_records = extract.extract_records(text, spec)
    except extract.ExtractionUnavailable as exc:
        db.update_document(document_id, **text_values)
        return _retry_or_fail(document, str(exc), llm_configured())

    normalized = extract.normalize(text)
    known = db.known_well_ids()
    checked = [extract.check_record(raw, spec, normalized, known) for raw in raw_records]
    accepted = [record for record in checked if record["accepted"]]
    mentioned = sorted({record["well_id"] for record in checked if record["well_id"]})
    dates = [record["fields"].get(load_date_field(spec), {}).get("value") for record in accepted]
    doc_date = next((value for value in dates if value), None)
    extraction = {"type_method": type_method, "model": settings.extraction_model,
                  "records": checked, "records_total": len(checked), "records_accepted": len(accepted)}
    if not accepted:
        issues = list(dict.fromkeys(issue for record in checked for issue in record["issues"]))
        message = ("Данные не подтверждены: " + " ".join(issues[:4])) if issues else \
            "Модель не нашла в документе нужных данных."
        return _finish(document_id, "not_recognized", message, doc_type=type_id, well_ids=mentioned,
                       extraction=extraction, loaded=None, doc_date=None, **text_values)

    loaded = load.load_records(document, spec, checked)
    wells = sorted(set(loaded["wells"]) | set(mentioned))
    _recompute(loaded["wells"], app_state)
    message = f"{spec.get('title')}: загружено записей — {loaded['records']}, скважин — {len(loaded['wells'])}."
    if len(accepted) < len(checked):
        message += f" Не подтверждено записей: {len(checked) - len(accepted)}."
    return _finish(document_id, "loaded", message, doc_type=type_id, well_ids=wells, extraction=extraction,
                   loaded=loaded, doc_date=doc_date and datetime.fromisoformat(doc_date).date(), **text_values)


def load_date_field(spec: dict) -> str:
    from app.core.documents.types import DATE_FIELD

    return DATE_FIELD[spec["target"]]


def process_queue(app_state=None, limit: int = 20) -> list[dict]:
    """Process up to `limit` queued documents; returns their final rows."""
    done = []
    for _ in range(limit):
        document = db.claim_next_document()
        if document is None:
            break
        try:
            done.append(process_document(document["document_id"], app_state))
        except Exception:
            logger.exception("Document processing failed document_id=%s", document["document_id"])
            done.append(_finish(document["document_id"], "failed", "Внутренняя ошибка обработки."))
    return done


def process_now(document_id: str, app_state=None) -> dict | None:
    """Process one queued document immediately (upload with wait=true); None when another
    worker has already taken it or it is not queued."""
    document = db.claim_next_document(document_id)
    if document is None:
        return None
    try:
        return process_document(document_id, app_state)
    except Exception:
        logger.exception("Document processing failed document_id=%s", document_id)
        return _finish(document_id, "failed", "Внутренняя ошибка обработки.")


def reset_for_processing(document_id: str) -> dict | None:
    if db.get_document(document_id) is None:
        return None
    db.update_document(document_id, status="queued", attempts=0, started_at=None,
                       status_message="Отправлен на повторную обработку.")
    return db.get_document(document_id)


def delete_document(document_id: str, app_state=None) -> dict | None:
    """Remove a document, the data it loaded and its stored original; recompute its wells."""
    document = db.get_document(document_id)
    if document is None:
        return None
    loaded = document.get("loaded") or {}
    db.delete_document_data(document_id, extra_batch_ids=loaded.get("batch_ids") or [])
    db.delete_document_row(document_id)
    try:
        (settings.docs_storage_dir / document["storage_path"]).unlink(missing_ok=True)
    except OSError:
        logger.warning("Stored original could not be removed document_id=%s", document_id)
    _recompute(loaded.get("wells") or [], app_state)
    return document


class DocumentWorker:
    """Background thread: scans the inbox folder and processes queued documents."""

    def __init__(self, app_state) -> None:
        self.app_state = app_state
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="documents-worker", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        last_scan = last_requeue = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if settings.docs_inbox_dir and now - last_scan >= settings.docs_inbox_scan_seconds:
                    last_scan = now
                    intake.scan_inbox()
                if now - last_requeue >= 60:
                    last_requeue = now
                    db.requeue_stale_documents()
                processed = process_queue(self.app_state, limit=5)
            except Exception:
                logger.exception("Documents worker cycle failed")
                processed = []
            if not processed:
                self._wake.wait(timeout=15)
                self._wake.clear()
