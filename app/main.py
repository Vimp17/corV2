from __future__ import annotations

import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app import db
from app.api.routes.analysis import router as analysis_router
from app.api.routes.ingestion import router as ingestion_router
from app.api.routes.monitoring import router as monitoring_router
from app.config import settings
from app.core.cox import load_optional_model
from app.core.model_registry import ModelRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mai_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    app.state.cox_model, app.state.cox_model_error = load_optional_model(settings.cox_model_path)
    app.state.risk_model_registry = ModelRegistry.with_builtins(
        app.state.cox_model, app.state.cox_model_error
    )
    if app.state.cox_model_error:
        logger.warning("Cox inference status: %s", app.state.cox_model_error)
    yield


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    expected = settings.api_key
    if expected and (not x_api_key or not secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=("Модульное вычислительное ядро контроля коррозии. Правила и пороги в этой версии "
                 "унаследованы из демонстрационного синтетического pipeline и требуют полевой калибровки."),
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID")
    if not request_id or len(request_id) > 128 or not request_id.isprintable():
        request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error request_id=%s path=%s", getattr(request.state, "request_id", "unknown"), request.url.path)
    return JSONResponse(status_code=500, content={
        "detail": "Internal server error",
        "request_id": getattr(request.state, "request_id", None),
    })


@app.get("/health/live", tags=["health"])
def live():
    return {"status": "alive", "service": settings.app_name, "version": settings.app_version}


@app.get("/health/ready", tags=["health"])
def ready():
    try:
        db.check_database()
    except Exception as exc:
        logger.warning("PostgreSQL readiness check failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="PostgreSQL database is unavailable") from exc
    return {
        "status": "ready", "storage": "postgresql", "database_connected": True,
        "cox_model": "loaded" if app.state.cox_model else "unavailable",
        "cox_model_detail": app.state.cox_model_error,
        "risk_models_registered": len(app.state.risk_model_registry.models),
        "llm_agent": "configured" if settings.llm_api_url and settings.llm_model else "not_configured",
    }


@app.get("/dashboard", include_in_schema=False)
@app.get("/dashboard/", include_in_schema=False)
def dashboard():
    page = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
    return FileResponse(page)


v1 = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
v1.include_router(ingestion_router)
v1.include_router(analysis_router)
v1.include_router(monitoring_router)
app.include_router(v1)
