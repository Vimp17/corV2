"""Documents: Excel, PDF, scans and photos uploaded by users or dropped into the inbox folder.

Each file goes through app.core.documents.pipeline: text (Excel, PDF text layer, OCR via
LiteLLM) -> document type -> fields with quotes (LLM via LiteLLM) -> checks -> source data ->
rating recompute. A document that cannot be confirmed loads nothing ("не распознан").
"""
from __future__ import annotations

import logging
import mimetypes

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app import db
from app.config import settings
from app.core.documents import intake, pipeline
from app.core.documents.text import ocr_configured
from app.core.documents.types import DocumentTypesError, load_document_types

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_FILES_PER_UPLOAD = 50
MAX_TEXT_IN_RESPONSE = 50_000


def _wake(request: Request) -> None:
    worker = getattr(request.app.state, "document_worker", None)
    if worker is not None:
        worker.wake()


async def _read_limited(upload: UploadFile) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.docs_max_bytes:
            raise intake.DocumentRejected(f"Файл больше {settings.docs_max_bytes // (1024 * 1024)} МБ.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("")
def list_documents(status: str | None = Query(default=None, max_length=32),
                   well_id: str | None = Query(default=None, max_length=128),
                   limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    titles = pipeline.type_titles()
    inbox = intake.inbox_path()
    return {
        "documents": [pipeline.decorate(row, titles) for row in db.list_documents(well_id=well_id, status=status, limit=limit)],
        "counts": db.document_status_counts(),
        "status_labels": pipeline.STATUS_LABELS,
        "types": [{"type_id": key, "title": value} for key, value in titles.items()],
        "settings": {
            "ocr_configured": ocr_configured(),
            "llm_configured": pipeline.llm_configured(),
            "inbox": str(inbox) if inbox else None,
            "inbox_scan_seconds": settings.docs_inbox_scan_seconds,
            "worker": settings.docs_worker_enabled,
            "max_bytes": settings.docs_max_bytes,
            "max_pages": settings.docs_max_pages,
            "accepted": sorted(intake.ACCEPTED_EXTENSIONS),
        },
    }


@router.post("", status_code=201)
async def upload_documents(request: Request, files: list[UploadFile] = File(...),
                           wait: bool = Query(default=False, description="Обработать сразу и вернуть результат")) -> dict:
    """Upload one or more files. Duplicates (same content) return the existing document."""
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(status_code=422, detail=f"Не больше {MAX_FILES_PER_UPLOAD} файлов за раз.")
    results = []
    for upload in files:
        name = intake.safe_filename(upload.filename)
        try:
            content = await _read_limited(upload)
            row = await run_in_threadpool(intake.accept_file, content, name, "upload")
        except intake.DocumentRejected as exc:
            results.append({"filename": name, "error": str(exc)})
            continue
        results.append(row)
    if wait:
        for index, row in enumerate(results):
            if row.get("document_id") and row.get("status") == "queued":
                processed = await run_in_threadpool(pipeline.process_now, row["document_id"], request.app.state)
                if processed:
                    results[index] = {**processed, "duplicate": row.get("duplicate", False),
                                      "requeued": row.get("requeued", False)}
    else:
        _wake(request)
    titles = pipeline.type_titles()
    return {"documents": [pipeline.decorate(row, titles) if row.get("document_id") else row for row in results],
            "accepted": sum(1 for row in results if row.get("document_id") and not row.get("duplicate")),
            "duplicates": sum(1 for row in results if row.get("duplicate") and not row.get("requeued")),
            "requeued": sum(1 for row in results if row.get("requeued")),
            "rejected": sum(1 for row in results if row.get("error"))}


@router.delete("")
def clear_documents(request: Request,
                    status: str | None = Query(default=None, max_length=200,
                                               description="Статусы через запятую, например failed,not_recognized"),
                    all_documents: bool = Query(default=False, alias="all")) -> dict:
    """Clear the document list: by status, or every document (?all=true). Data loaded from the
    deleted documents is deleted too and the rating of their wells is recomputed. Documents in
    the queue or being processed are kept."""
    if all_documents:
        statuses = None
    else:
        statuses = {item.strip() for item in (status or "").split(",") if item.strip()}
        unknown = statuses - set(pipeline.STATUS_LABELS)
        if not statuses or unknown:
            raise HTTPException(status_code=422, detail="Укажите status (например failed,not_recognized) или all=true.")
    return pipeline.delete_documents(statuses, request.app.state)


@router.post("/inbox/scan")
def scan_inbox(request: Request) -> dict:
    """Take new files from the inbox folder now (the worker also does it periodically)."""
    if not settings.docs_inbox_dir:
        raise HTTPException(status_code=409, detail="Папка-приёмник не настроена (MAI_DOCS_INBOX_DIR).")
    summary = intake.scan_inbox()
    _wake(request)
    return summary


@router.post("/process-queue")
def process_queue(request: Request, limit: int = Query(default=20, ge=1, le=200)) -> dict:
    """Process queued documents synchronously (for n8n, or when MAI_DOCS_WORKER=false)."""
    titles = pipeline.type_titles()
    return {"documents": [pipeline.decorate(row, titles) for row in pipeline.process_queue(request.app.state, limit)]}


@router.get("/{document_id}")
def get_document(document_id: str, include_text: bool = Query(default=False)) -> dict:
    row = db.get_document(document_id, with_text=include_text)
    if row is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if include_text and row.get("text") and len(row["text"]) > MAX_TEXT_IN_RESPONSE:
        row["text"] = row["text"][:MAX_TEXT_IN_RESPONSE]
        row["text_truncated"] = True
    try:
        types = load_document_types()
    except DocumentTypesError:
        types = {}
    spec = types.get(row.get("doc_type") or "")
    row["field_labels"] = ({name: field.get("label", name) for name, field in spec["fields"].items()}
                           if spec else dict((row.get("extraction") or {}).get("field_labels") or {}))
    return pipeline.decorate(row, {key: value.get("title") or key for key, value in types.items()})


@router.get("/{document_id}/file")
def get_original(document_id: str) -> FileResponse:
    row = db.get_document(document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    path = settings.docs_storage_dir / row["storage_path"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Оригинал файла не найден в хранилище")
    media_type = mimetypes.guess_type(row["filename"])[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=row["filename"])


@router.post("/{document_id}/reprocess")
async def reprocess_document(document_id: str, request: Request, wait: bool = Query(default=False)) -> dict:
    row = await run_in_threadpool(pipeline.reset_for_processing, document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if wait:
        processed = await run_in_threadpool(pipeline.process_now, document_id, request.app.state)
        row = processed or row
    else:
        _wake(request)
    return pipeline.decorate(row, pipeline.type_titles())


@router.delete("/{document_id}")
def delete_document(document_id: str, request: Request) -> dict:
    """Delete the document and the data loaded from it; the rating of its wells is recomputed."""
    row = pipeline.delete_document(document_id, request.app.state)
    if row is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    return {"deleted": document_id, "wells": (row.get("loaded") or {}).get("wells") or []}
