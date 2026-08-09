"""Endpoints de descarga.

A diferencia del original (`SUBTITLES_DIR / filename` con el filename
tomado directo de la URL), acá el único input del usuario es `job_id`, que
se usa exclusivamente para consultar Celery. El nombre real del archivo y
la key en el bucket salen del resultado que guardó el worker — nunca del
request — así que no hay superficie para path traversal.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.celery_client import get_async_result
from app.config import get_settings
from app.storage import object_exists, presigned_download_url

router = APIRouter(prefix="/api", tags=["downloads"])
settings = get_settings()


def _completed_result(job_id: str) -> dict[str, Any]:
    async_result = get_async_result(job_id)
    if async_result.state != "SUCCESS":
        raise HTTPException(status_code=404, detail="El trabajo no existe o todavía no terminó")
    result = async_result.result
    if not isinstance(result, dict):
        raise HTTPException(status_code=404, detail="El trabajo no tiene resultado disponible")
    return result


@router.get("/jobs/{job_id}/download/srt")
async def download_srt(job_id: str) -> dict[str, str]:
    result = _completed_result(job_id)
    key, filename = result.get("srt_key"), result.get("srt_filename", "subtitulos.srt")
    if not key or not object_exists(settings, key):
        raise HTTPException(status_code=404, detail="Archivo de subtítulos no encontrado")
    return {"url": presigned_download_url(settings, key, filename)}


@router.get("/jobs/{job_id}/download/video")
async def download_video(job_id: str) -> dict[str, str]:
    result = _completed_result(job_id)
    key, filename = result.get("video_key"), result.get("video_filename", "video.mp4")
    if not key or not object_exists(settings, key):
        raise HTTPException(status_code=404, detail="Video no encontrado")
    return {"url": presigned_download_url(settings, key, filename)}
