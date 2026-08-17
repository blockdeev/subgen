"""App de Celery. Redis como broker Y como result backend (un solo servicio
menos que correr, y alcanza sobradamente para el volumen de este proyecto)."""
from __future__ import annotations

from celery import Celery

from app.config import get_settings
from app.logging_config import configure_logging
from app.storage import ensure_bucket


def resolve_max_tasks_per_child(configured: int) -> int | None:
    """`configured <= 0` deshabilita la mitigación (comportamiento viejo,
    sin límite). `configured > 0` fuerza un proceso nuevo del worker cada
    esa cantidad de tareas -- ver ARCHITECTURE.md "Problemas conocidos"."""
    return configured if configured > 0 else None


settings = get_settings()
configure_logging(settings.log_level)

try:
    ensure_bucket(settings)
except Exception:  # noqa: BLE001 - no debe tumbar el arranque del worker
    import logging

    logging.getLogger(__name__).warning(
        "No se pudo verificar/crear el bucket S3 al arrancar (¿MinIO todavía no está listo?)"
    )

celery_app = Celery("subgen", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # acks_late + prefetch=1: si un worker se cae a mitad de una tarea, la
    # tarea vuelve a la cola y la toma otro worker en vez de perderse —
    # pero SOLO si visibility_timeout (abajo) está bien configurado. Sin
    # eso, Redis usa su default de 3600s antes de considerar "perdido" el
    # mensaje no confirmado, dejando la tarea en limbo hasta una hora
    # después de un crash real (confirmado en producción: un worker se
    # cayó a mitad de un quemado largo, y el job quedó mostrando el último
    # progreso conocido sin que nadie lo retomara).
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=settings.celery_task_time_limit,
    task_soft_time_limit=settings.celery_task_soft_time_limit,
    result_expires=settings.cleanup_max_age_hours * 3600,
    # Tiene que ser mayor al tiempo máximo que puede durar una tarea
    # (si no, Redis redistribuye el mensaje a otro worker MIENTRAS la
    # tarea original todavía está corriendo legítimamente) pero no mucho
    # más que eso — si no, un crash real tarda demasiado en recuperarse.
    broker_transport_options={
        "visibility_timeout": settings.celery_task_time_limit + 300,
    },
    # Mitigación de threads huérfanos de ctranslate2/FFmpeg -- ver
    # ARCHITECTURE.md "Problemas conocidos" para el detalle completo y por
    # qué esto es mitigación, no arreglo de causa raíz. None = deshabilitado.
    worker_max_tasks_per_child=resolve_max_tasks_per_child(settings.celery_max_tasks_per_child),
    beat_schedule={
        "cleanup-expired-outputs": {
            "task": "app.tasks.cleanup_expired_outputs",
            "schedule": settings.cleanup_interval_seconds,
        },
    },
)

celery_app.autodiscover_tasks(["app"])
