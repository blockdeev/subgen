"""Fixtures compartidas.

Solo imports livianos (stdlib + pytest) a nivel de módulo a propósito: este
archivo se carga SIEMPRE que pytest corre en `tests/`, incluso cuando lo
que se está corriendo son los tests del worker (que no tienen fastapi
instalado). Todo lo que dependa de la API se importa adentro de cada
fixture, nunca acá arriba.
"""
from __future__ import annotations

import os

# Tiene que setearse ANTES de que se importe cualquier cosa de `app.*`,
# porque `get_settings()` usa `lru_cache` y varios módulos de la API leen
# la config a nivel de módulo en el momento del import.
os.environ.setdefault("SUBGEN_CELERY_TASK_ALWAYS_EAGER", "true")
os.environ.setdefault("SUBGEN_RATE_LIMIT_CREATE_JOB", "5/minute")
os.environ.setdefault("SUBGEN_CORS_ORIGINS", "http://localhost:8000")
os.environ.setdefault("SUBGEN_SERVE_FRONTEND", "false")

import pytest  # noqa: E402


@pytest.fixture
def stub_task_result():
    """Dict mutable: lo que la tarea stub devuelve como resultado exitoso."""
    return {}


@pytest.fixture
def stub_task_error():
    """Dict con la excepción que la tarea stub debe lanzar, si se setea `exc`."""
    return {"exc": None}


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """El limiter de slowapi es un singleton en memoria a nivel de módulo
    (`app.rate_limit.limiter`) que persiste estado ENTRE tests dentro del
    mismo proceso -- sin esto, cuántas veces pasó un test antes afecta si
    el siguiente choca con el límite, dependiendo del orden de ejecución
    (nos pasó de verdad: agregar 2 tests nuevos hizo fallar otros que
    nunca tocaron rate limiting). Se resetea antes de CADA test, así el
    límite de 5/minute siempre arranca en cero salvo que el test mismo
    lo agote a propósito (como hace TestRateLimit).
    """
    try:
        from app.rate_limit import limiter
        limiter.limiter.storage.reset()
    except Exception:
        pass  # en los tests del worker no existe app.rate_limit -- no pasa nada
    yield


@pytest.fixture
def register_stub_task(stub_task_result, stub_task_error):
    """Registra, bajo el mismo nombre que usa la API para encolar
    (`app.tasks.process_video`), una tarea *stub* liviana en el propio
    Celery app productor de la API — sin importar el paquete pesado del
    worker (faster-whisper, ffmpeg, etc.). Con `task_always_eager=True` más
    un backend en memoria, esto alcanza para probar el flujo completo
    create -> poll sin ninguna infraestructura real.
    """
    from app.celery_client import TASK_NAME, celery_client

    celery_client.conf.task_always_eager = True
    celery_client.conf.task_store_eager_result = True
    celery_client.conf.result_backend = "cache+memory://"

    @celery_client.task(name=TASK_NAME, bind=True)
    def _stub(self, job_id, url, target_lang, mode, output_fps=30):
        if stub_task_error.get("exc") is not None:
            raise stub_task_error["exc"]
        return dict(stub_task_result)

    yield _stub

    celery_client.tasks.pop(TASK_NAME, None)


@pytest.fixture
def fake_redis_ok(monkeypatch):
    import app.routes.health as health_module

    class _FakeRedis:
        def ping(self):
            return True

    monkeypatch.setattr(health_module.redis, "from_url", lambda *a, **kw: _FakeRedis())


@pytest.fixture
def fake_redis_down(monkeypatch):
    import app.routes.health as health_module

    class _FakeRedis:
        def ping(self):
            raise ConnectionError("no se pudo conectar")

    monkeypatch.setattr(health_module.redis, "from_url", lambda *a, **kw: _FakeRedis())
