"""Health check para Docker healthchecks y monitoreo externo."""
from __future__ import annotations

import redis
from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(tags=["health"])
settings = get_settings()


@router.get("/health")
async def health() -> dict[str, object]:
    redis_ok = True
    try:
        redis.from_url(settings.redis_url, socket_connect_timeout=2).ping()
    except Exception:
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded", "redis": redis_ok}
