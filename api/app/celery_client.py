"""Cliente Celery del lado de la API (productor).

Deliberadamente NO importa `worker.app.tasks`: la API no tiene (ni debe
tener) faster-whisper, deep-translator ni FFmpeg instalados. Alcanza con
conocer el nombre de la tarea y compartir el mismo broker/backend (Redis)
que el worker para encolar por nombre y consultar `AsyncResult` por id.
"""
from __future__ import annotations

from celery import Celery
from celery.result import AsyncResult
from kombu.exceptions import OperationalError

from app.config import get_settings

settings = get_settings()

TASK_NAME = "app.tasks.process_video"


class QueueUnavailableError(RuntimeError):
    """La cola (Redis) no está disponible. Distinto de un error interno
    genérico: el request del usuario era válido, es la infraestructura la
    que está caída — vale un 503 retryable, no un 500 plano."""

celery_client = Celery("subgen_client", broker=settings.redis_url, backend=settings.redis_url)
celery_client.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_always_eager=settings.celery_task_always_eager,
)


def enqueue_job(job_id: str, url: str, target_lang: str, mode: str, output_fps: int = 30) -> None:
    # OJO: `celery_client.send_task(...)` ignora `task_always_eager` (Celery
    # emite el warning `AlwaysEagerIgnored` y manda igual por la red) porque
    # send_task() está pensado justo para el caso "no tengo la tarea
    # registrada acá". `Signature.apply_async()` en cambio SÍ respeta el
    # modo eager: usa el Task real si está registrado localmente (nuestros
    # tests registran un stub bajo el mismo nombre) y cae a send_task()
    # si no lo está (el caso real en producción, donde la API nunca
    # importa el código del worker). Mismo comportamiento en prod,
    # testeable en eager mode.
    #
    # Verificado en vivo (levantando la API real sin Redis corriendo): si
    # el broker/backend no responde, Celery termina lanzando un RuntimeError
    # genérico ("Retry limit exceeded...") después de agotar sus propios
    # reintentos de conexión — lo traducimos a QueueUnavailableError para
    # que la ruta pueda devolver 503 (reintentable) en vez de un 500 plano.
    try:
        celery_client.signature(
            TASK_NAME, args=[job_id, url, target_lang, mode, output_fps], task_id=job_id,
        ).apply_async()
    except (OperationalError, RuntimeError, ConnectionError, OSError) as exc:
        raise QueueUnavailableError(f"No se pudo encolar el trabajo: {exc}") from exc


def get_async_result(job_id: str) -> AsyncResult:
    return AsyncResult(job_id, app=celery_client)
