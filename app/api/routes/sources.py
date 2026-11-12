"""Read-only connection diagnostics; endpoints are configured by the local administrator."""
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.core import source_connectors as sources
from app.db.sources import latest_source_statuses

router = APIRouter(prefix="/sources", tags=["sources"])


class SourceCheck(BaseModel):
    well_id: str = Field(min_length=1, max_length=128, pattern=r".*\S.*")


@router.get("")
def overview():
    latest = latest_source_statuses()
    rows = []
    for mode in sources.source_modes():
        last = latest.get(mode["source_id"])
        if last:
            last = {key: last[key] for key in ("status", "origin", "well_id", "retrieved_at", "record_count", "message")}
            message = str(last.get("message") or "")
            if token := sources.source_env(mode["source_id"], "TOKEN"):
                message = message.replace(token, "[скрыто]")
            last["message"] = re.sub(r"https?://\S+", "[адрес скрыт]", message)[:2000]
        rows.append({**mode, "last_attempt": last})
    build = {}
    try:
        info = json.loads((Path(__file__).resolve().parents[3] / "BUILD_INFO.json").read_text())
        build = {key: info.get(key) for key in ("git_commit", "built_at", "python")}
    except (OSError, ValueError):
        pass
    return {"sources": rows, "version": settings.app_version, "build": build,
            "configured_well_count": len(settings.configured_well_ids)}


@router.post("/{profile_id}/check")
def check(profile_id: str, request: SourceCheck):
    try:
        return sources.check_source(profile_id, request.well_id.strip())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
