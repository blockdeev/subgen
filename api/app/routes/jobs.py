"""Endpoints de creación y consulta de estado de trabajos."""
from __future__ import annotations

import uuid

from celery.result import AsyncResult
from fastapi import APIRouter, Request

from app.celery_client import enqueue_job, get_async_result
from app.config import get_settings
from app.models import JobCreateRequest, JobCreateResponse, JobResult, JobStatusResponse
from app.rate_limit import limiter

router = APIRouter(prefix="/api", tags=["jobs"])
settings = get_settings()


@router.post("/jobs", response_model=JobCreateResponse, status_code=202)
@limiter.limit(settings.rate_limit_create_job)
async def create_job(request: Request, payload: JobCreateRequest) -> JobCreateResponse:
    job_id = uuid.uuid4().hex[:12]
    enqueue_job(job_id, payload.url, payload.target_lang, payload.mode)
    return JobCreateResponse(job_id=job_id)


def map_async_result(async_result: AsyncResult) -> JobStatusResponse:
    """Traduce el estado nativo de Celery a nuestro esquema de respuesta.

    PROGRESS trae nuestro payload rico (mismo que se publica por Redis
    Pub/Sub al WebSocket). FAILURE, en cambio, solo tiene la excepción tal
    cual Celery la guardó — por eso el mensaje que da este endpoint REST es
    más genérico que el que recibe el cliente por WebSocket. Está
    documentado así en el README: el WS es la vía "rica", el REST es el
    fallback.
    """
    state = async_result.state
    job_id = async_result.id

    if state == "PENDING":
        return JobStatusResponse(job_id=job_id, status="queued", progress=0.0)

    if state == "PROGRESS":
        meta = async_result.info if isinstance(async_result.info, dict) else {}
        return JobStatusResponse(
            job_id=job_id,
            status=meta.get("status", "processing"),
            progress=float(meta.get("progress", 0.0)),
            stage_progress=meta.get("stage_progress"),
            message_key=meta.get("message_key"),
            message_params=meta.get("message_params", {}),
            eta_seconds=meta.get("eta_seconds"),
        )

    if state == "SUCCESS":
        result = async_result.result or {}
        return JobStatusResponse(
            job_id=job_id, status="completed", progress=100.0, result=JobResult(**result)
        )

    if state == "FAILURE":
        return JobStatusResponse(
            job_id=job_id, status="error", progress=0.0,
            message_key="status.error_generic",
            message_params={"detail": str(async_result.info)},
        )

    if state == "RETRY":
        return JobStatusResponse(job_id=job_id, status="retrying", progress=0.0)

    return JobStatusResponse(job_id=job_id, status=state.lower(), progress=0.0)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str) -> JobStatusResponse:
    return map_async_result(get_async_result(job_id))
