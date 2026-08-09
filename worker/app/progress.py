"""Publica eventos de progreso en Redis Pub/Sub.

Decisión de arquitectura (ver README, "Cómo se empuja el progreso"): en vez
de que la API haga polling interno del backend de resultados de Celery, el
worker publica cada actualización a un canal `progress:{job_id}` y la API
se suscribe a ese canal por cada WebSocket conectado. Esto da progreso en
tiempo real de verdad, no polling oculto.

El mismo payload que se publica acá es el que se guarda con
`self.update_state(...)`, así el endpoint REST de fallback (`GET /jobs/{id}`)
y el WebSocket siempre muestran el mismo estado.
"""
from __future__ import annotations

import json
from typing import Any

import redis


def channel_name(job_id: str) -> str:
    return f"progress:{job_id}"


class ProgressPublisher:
    def __init__(self, redis_url: str) -> None:
        self._client = redis.from_url(redis_url)

    def publish(self, job_id: str, payload: dict[str, Any]) -> None:
        self._client.publish(channel_name(job_id), json.dumps(payload))
