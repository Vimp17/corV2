"""Receiving document files: manual upload or the inbox folder.

A file is checked (type by its content, size), stored once by its SHA-256 and queued. The same
file sent again returns the existing document instead of a second copy. Inbox files are moved
to processed/<date>/ or failed/<date>/ next to the inbox after they are taken.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import db
from app.config import settings

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
ACCEPTED_EXTENSIONS = {".pdf", ".xlsx", ".xls"} | IMAGE_EXTENSIONS
_SKIP_SUFFIXES = (".tmp", ".part", ".crdownload", ".download", ".error.txt")
# An inbox file still being copied is left for the next scan.
_SETTLE_SECONDS = 5


# Statuses a document leaves when the same file is uploaded again.
RETRY_ON_REUPLOAD = {"failed", "not_recognized"}


class DocumentRejected(ValueError):
    """The file cannot be accepted (type, size); the message is shown to the user."""


def safe_filename(name: str | None) -> str:
    base = Path(str(name or "document")).name
    base = "".join(char for char in base if char.isprintable() and char not in '<>:"/\\|?*')
    return base.strip()[:255] or "document"


def detect_kind(content: bytes, filename: str) -> str:
    """pdf / xlsx / xls / image by the file's content; the extension must agree for Excel."""
    suffix = Path(filename).suffix.casefold()
    head = content[:16]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        if suffix == ".xlsx":
            return "xlsx"
        raise DocumentRejected("Архив или файл Office не поддерживается: загрузите .xlsx, .xls, PDF или изображение.")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # Excel 97-2003: converted to .xlsx when processed (app.core.excel_legacy).
        if suffix == ".xls":
            return "xls"
        raise DocumentRejected("Файлы .doc не поддерживаются: сохраните документ как PDF.")
    if (head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n")
            or head.startswith(b"II*\x00") or head.startswith(b"MM\x00*") or head.startswith(b"BM")
            or (head.startswith(b"RIFF") and content[8:12] == b"WEBP")):
        return "image"
    raise DocumentRejected("Неподдерживаемый формат: принимаются PDF, Excel (.xlsx, .xls) и изображения (JPG, PNG, TIFF).")


def storage_path_for(sha256: str, filename: str) -> Path:
    suffix = Path(filename).suffix.casefold()
    suffix = suffix if suffix in ACCEPTED_EXTENSIONS else ".bin"
    return Path(sha256[:2]) / f"{sha256}{suffix}"


def read_original(document: dict) -> bytes:
    return (settings.docs_storage_dir / document["storage_path"]).read_bytes()


def accept_file(content: bytes, filename: str, channel: str) -> dict:
    """Store and queue one file. Returns the document row plus "duplicate": True/False."""
    filename = safe_filename(filename)
    if not content:
        raise DocumentRejected("Файл пустой.")
    if len(content) > settings.docs_max_bytes:
        raise DocumentRejected(f"Файл больше {settings.docs_max_bytes // (1024 * 1024)} МБ.")
    kind = detect_kind(content, filename)
    if kind in {"xlsx", "xls"} and len(content) > settings.max_excel_bytes:
        raise DocumentRejected(f"Таблица больше {settings.max_excel_bytes // (1024 * 1024)} МБ.")
    sha256 = hashlib.sha256(content).hexdigest()
    relative = storage_path_for(sha256, filename)
    target = settings.docs_storage_dir / relative
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.part")
        temporary.write_bytes(content)
        temporary.replace(target)
    row, created = db.create_document({
        "document_id": str(uuid.uuid4()), "sha256": sha256, "filename": filename,
        "channel": channel, "file_kind": kind, "size_bytes": len(content),
        "storage_path": relative.as_posix(), "status": "queued", "attempts": 0,
        "created_at": datetime.now(timezone.utc),
    })
    requeued = False
    if not created and row.get("status") in RETRY_ON_REUPLOAD:
        # The same file sent again after it failed or was not recognised (e.g. before OCR or the
        # Excel reader was set up) is processed again instead of returning the old result.
        db.update_document(row["document_id"], status="queued", attempts=0, started_at=None,
                           filename=filename, status_message="Загружен повторно — обрабатывается заново.")
        row = db.get_document(row["document_id"]) or row
        requeued = True
    return {**row, "duplicate": not created, "requeued": requeued}


def _move(path: Path, folder: str, reason: str | None = None) -> Path:
    day = datetime.now().strftime("%Y-%m-%d")
    target_dir = path.parent / folder / day
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    counter = 1
    while target.exists():
        target = target_dir / f"{path.stem} ({counter}){path.suffix}"
        counter += 1
    shutil.move(str(path), target)
    if reason:
        target.with_name(target.name + ".error.txt").write_text(reason + "\n", encoding="utf-8")
    return target


def _safe_move(path: Path, folder: str, reason: str | None = None) -> None:
    """Move a taken file; another worker may have moved it already (the content is deduplicated)."""
    try:
        _move(path, folder, reason)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Inbox file could not be moved name=%s folder=%s", path.name, folder, exc_info=True)


def inbox_path() -> Path | None:
    if not settings.docs_inbox_dir:
        return None
    path = Path(settings.docs_inbox_dir).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path)


def scan_inbox() -> dict:
    """Take every settled file from the inbox folder. Safe to call from several workers: a file
    already moved by another worker is skipped, and duplicates are recognised by content."""
    folder = inbox_path()
    summary = {"inbox": str(folder) if folder else None, "found": 0, "accepted": 0,
               "duplicates": 0, "rejected": 0, "documents": []}
    if folder is None:
        return summary
    if not folder.is_dir():
        summary["error"] = "Папка-приёмник не найдена"
        return summary
    now = time.time()
    for path in sorted(folder.iterdir()):
        name = path.name
        if (not path.is_file() or name.startswith((".", "~$"))
                or name.casefold().endswith(_SKIP_SUFFIXES)):
            continue
        try:
            if now - path.stat().st_mtime < _SETTLE_SECONDS:
                continue
            content = path.read_bytes()
        except FileNotFoundError:
            continue
        summary["found"] += 1
        try:
            if path.suffix.casefold() not in ACCEPTED_EXTENSIONS:
                raise DocumentRejected("Неподдерживаемое расширение файла.")
            row = accept_file(content, name, "inbox")
        except DocumentRejected as exc:
            summary["rejected"] += 1
            _safe_move(path, "failed", str(exc))
            logger.info("Inbox file rejected name=%s reason=%s", name, exc)
            continue
        except Exception:
            logger.exception("Inbox file could not be taken name=%s", name)
            continue
        _safe_move(path, "processed")
        summary["duplicates" if row["duplicate"] and not row["requeued"] else "accepted"] += 1
        summary["documents"].append({"document_id": row["document_id"], "filename": row["filename"],
                                     "duplicate": row["duplicate"], "status": row["status"]})
    return summary


__all__ = ["DocumentRejected", "accept_file", "detect_kind", "read_original", "scan_inbox",
           "inbox_path", "safe_filename", "IMAGE_EXTENSIONS", "ACCEPTED_EXTENSIONS"]
