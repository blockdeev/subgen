"""Punto de entrada de la API FastAPI."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.celery_client import QueueUnavailableError
from app.config import get_settings
from app.logging_config import configure_logging
from app.rate_limit import limiter
from app.routes import downloads, health, jobs
from app.websocket import router as websocket_router

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="SubGen API", version="1.0.0")

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(jobs.router)
app.include_router(downloads.router)
app.include_router(websocket_router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Mensajes de Pydantic tal cual (son seguros: describen el input, no
    # rutas ni internals) pero sin traceback.
    first_error = exc.errors()[0] if exc.errors() else {}
    return JSONResponse(status_code=422, content={"error": first_error.get("msg", "Datos inválidos")})


@app.exception_handler(QueueUnavailableError)
async def queue_unavailable_handler(request: Request, exc: QueueUnavailableError) -> JSONResponse:
    logger.error("Cola no disponible al procesar %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"error": "El servicio de procesamiento no está disponible en este momento. Probá de nuevo en unos segundos."},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Nunca devolver stack traces ni rutas internas del servidor al cliente.
    logger.exception("Error no manejado en %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": "Error interno del servidor"})


if settings.serve_frontend:
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
