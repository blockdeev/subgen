"""WebSocket de progreso en tiempo real.

No hace polling: se suscribe al canal `progress:{job_id}` de Redis (al que
publica el worker desde `ProgressPublisher`, ver worker/app/progress.py) y
reenvía cada mensaje tal cual llega. `GET /api/jobs/{id}` sigue disponible
como fallback si el WebSocket no puede conectar.
"""
from __future__ import annotations

import asyncio
import json
import logging

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.celery_client import get_async_result
from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

_TERMINAL_STATUSES = {"completed", "error"}


@router.websocket("/ws/jobs/{job_id}")
async def job_progress_ws(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()
    redis_client = aioredis.from_url(settings.redis_url)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"progress:{job_id}")

    async def send_current_snapshot() -> None:
        """Por si el job ya avanzó antes de que el cliente abriera el WS."""
        async_result = get_async_result(job_id)
        if async_result.state == "PENDING":
            return
        info = async_result.info if isinstance(async_result.info, dict) else {}
        await websocket.send_json({
            "job_id": job_id,
            "status": info.get("status", async_result.state.lower()),
            **{k: v for k, v in info.items() if k != "status"},
        })

    async def forward_progress() -> None:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            payload = json.loads(message["data"])
            await websocket.send_json(payload)
            if payload.get("status") in _TERMINAL_STATUSES:
                return

    async def watch_disconnect() -> None:
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return

    try:
        await send_current_snapshot()
    except Exception:
        logger.exception("No se pudo enviar el snapshot inicial para job %s", job_id)

    forward_task = asyncio.create_task(forward_progress())
    disconnect_task = asyncio.create_task(watch_disconnect())
    try:
        await asyncio.wait({forward_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        forward_task.cancel()
        disconnect_task.cancel()
        await pubsub.unsubscribe()
        await pubsub.close()
        await redis_client.close()
