"""Tests de integración de la API. Corren con Celery en modo eager (una
tarea *stub* registrada bajo el mismo nombre que usa la API para encolar,
`app.tasks.process_video`, sin importar el pipeline pesado del worker) y
Redis mockeado para /api/health. Correr desde `api/`: `pytest ../tests/test_api.py`.

IMPORTANTE: `SUBGEN_CELERY_TASK_ALWAYS_EAGER=true` se setea en conftest.py
ANTES de que se importe cualquier cosa de `app`, porque `get_settings()`
está cacheado con `lru_cache` y varios módulos leen la config a nivel de
módulo (al importarse).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(register_stub_task):
    from app.main import app

    with TestClient(app) as c:
        yield c


class TestCreateJobValidation:
    def test_rejects_missing_url(self, client):
        resp = client.post("/api/jobs", json={})
        assert resp.status_code == 422

    def test_rejects_non_http_scheme(self, client):
        resp = client.post("/api/jobs", json={"url": "ftp://example.com/video"})
        assert resp.status_code == 422

    def test_rejects_url_without_domain(self, client):
        resp = client.post("/api/jobs", json={"url": "https://"})
        assert resp.status_code == 422

    def test_rejects_unsupported_target_lang(self, client):
        resp = client.post(
            "/api/jobs",
            json={"url": "https://youtube.com/watch?v=abc123", "target_lang": "xx"},
        )
        assert resp.status_code == 422

    def test_rejects_unsupported_output_fps(self, client):
        resp = client.post(
            "/api/jobs",
            json={"url": "https://youtube.com/watch?v=abc123", "output_fps": 45},
        )
        assert resp.status_code == 422

    def test_accepts_output_fps_30_and_60(self, client, stub_task_result):
        for fps in (30, 60):
            resp = client.post(
                "/api/jobs",
                json={"url": "https://youtube.com/watch?v=abc123", "output_fps": fps},
            )
            assert resp.status_code == 202

    def test_rejects_invalid_mode(self, client):
        resp = client.post(
            "/api/jobs",
            json={"url": "https://youtube.com/watch?v=abc123", "mode": "not-a-mode"},
        )
        assert resp.status_code == 422

    def test_error_response_never_leaks_stack_trace(self, client):
        resp = client.post("/api/jobs", json={"url": "not-a-url"})
        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        assert "Traceback" not in body["error"]
        assert "/api/" not in body["error"]  # sin rutas internas


class TestJobLifecycleHappyPath:
    def test_create_and_fetch_completed_job(self, client, stub_task_result):
        stub_task_result.update({
            "title": "Un video de prueba",
            "duration": 42.0,
            "segments_count": 3,
            "srt_key": "job123/video_es.srt",
            "srt_filename": "video_es.srt",
            "mode": "srt",
            "preview_segments": [],
        })

        resp = client.post("/api/jobs", json={"url": "https://youtube.com/watch?v=abc123", "target_lang": "es"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        assert job_id

        status_resp = client.get(f"/api/jobs/{job_id}")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["status"] == "completed"
        assert data["progress"] == 100.0
        assert data["result"]["title"] == "Un video de prueba"
        assert data["result"]["segments_count"] == 3


class TestJobLifecycleErrorPath:
    def test_create_and_fetch_failed_job(self, client, stub_task_error):
        stub_task_error["exc"] = RuntimeError("No se detectó habla en el audio")

        resp = client.post("/api/jobs", json={"url": "https://youtube.com/watch?v=abc123"})
        job_id = resp.json()["job_id"]

        status_resp = client.get(f"/api/jobs/{job_id}")
        data = status_resp.json()
        assert data["status"] == "error"
        assert "No se detectó habla" in data["message_params"]["detail"]


class TestJobNotFound:
    def test_unknown_job_id_returns_queued_not_404(self, client):
        # Celery no distingue "no existe" de "todavía no arrancó": un
        # job_id inventado queda en PENDING, igual que uno recién encolado
        # que el worker todavía no tomó. Es una limitación conocida del
        # backend elegido, documentada en el README.
        resp = client.get("/api/jobs/un-id-que-no-existe")
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"


class TestDownloads:
    def test_download_before_completion_returns_404(self, client):
        resp = client.get("/api/jobs/un-job-que-no-terminó/download/srt")
        assert resp.status_code == 404

    def test_download_video_before_completion_returns_404(self, client):
        resp = client.get("/api/jobs/un-job-que-no-terminó/download/video")
        assert resp.status_code == 404

    def test_successful_srt_download_returns_presigned_url(self, client, stub_task_result, monkeypatch):
        import app.routes.downloads as downloads_module

        stub_task_result.update({
            "title": "t", "duration": 1.0, "segments_count": 1, "mode": "srt",
            "srt_key": "job1/out_es.srt", "srt_filename": "out_es.srt", "preview_segments": [],
        })
        monkeypatch.setattr(downloads_module, "object_exists", lambda *a, **kw: True)
        monkeypatch.setattr(
            downloads_module, "presigned_download_url",
            lambda settings, key, filename: f"http://minio/{key}?sig=fake",
        )

        job_id = client.post("/api/jobs", json={"url": "https://youtube.com/watch?v=abc"}).json()["job_id"]
        resp = client.get(f"/api/jobs/{job_id}/download/srt")

        assert resp.status_code == 200
        assert resp.json()["url"] == "http://minio/job1/out_es.srt?sig=fake"

    def test_download_missing_object_returns_404_even_if_job_completed(self, client, stub_task_result, monkeypatch):
        import app.routes.downloads as downloads_module

        stub_task_result.update({
            "title": "t", "duration": 1.0, "segments_count": 1, "mode": "srt",
            "srt_key": "job1/out_es.srt", "srt_filename": "out_es.srt", "preview_segments": [],
        })
        monkeypatch.setattr(downloads_module, "object_exists", lambda *a, **kw: False)

        job_id = client.post("/api/jobs", json={"url": "https://youtube.com/watch?v=abc"}).json()["job_id"]
        resp = client.get(f"/api/jobs/{job_id}/download/srt")
        assert resp.status_code == 404

    def test_video_download_404_when_mode_was_srt_only(self, client, stub_task_result):
        # Un job en modo "srt" no tiene video_key: pedir /download/video
        # tiene que dar 404, no un 500 por KeyError.
        stub_task_result.update({
            "title": "t", "duration": 1.0, "segments_count": 1, "mode": "srt",
            "srt_key": "job1/out_es.srt", "srt_filename": "out_es.srt", "preview_segments": [],
        })
        job_id = client.post("/api/jobs", json={"url": "https://youtube.com/watch?v=abc"}).json()["job_id"]
        resp = client.get(f"/api/jobs/{job_id}/download/video")
        assert resp.status_code == 404


class TestHealth:
    def test_health_ok_with_reachable_redis(self, client, fake_redis_ok):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_degraded_when_redis_unreachable(self, client, fake_redis_down):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"


class TestRateLimit:
    def test_create_job_rate_limit_returns_429_eventually(self, client, stub_task_result):
        # El límite por default es SUBGEN_RATE_LIMIT_CREATE_JOB=5/minute
        # (ver conftest.py). Mandamos de más para forzar el 429.
        last_status = None
        for _ in range(10):
            resp = client.post("/api/jobs", json={"url": "https://youtube.com/watch?v=abc123"})
            last_status = resp.status_code
            if last_status == 429:
                break
        assert last_status == 429


class TestCancelJob:
    def test_cancel_running_job_calls_revoke_with_sigkill_and_returns_cancelled_true(self, client, monkeypatch):
        import app.celery_client as celery_client_module

        revoke_calls = []
        monkeypatch.setattr(
            celery_client_module.celery_client.control, "revoke",
            lambda job_id, **kw: revoke_calls.append((job_id, kw)),
        )

        resp = client.post("/api/jobs/un-job-corriendo/cancel")

        assert resp.status_code == 200
        data = resp.json()
        assert data["cancelled"] is True
        assert data["job_id"] == "un-job-corriendo"
        assert len(revoke_calls) == 1
        called_job_id, called_kwargs = revoke_calls[0]
        assert called_job_id == "un-job-corriendo"
        assert called_kwargs["terminate"] is True
        assert called_kwargs["signal"] == "SIGKILL"

    def test_cancel_already_completed_job_returns_cancelled_false(self, client, stub_task_result, monkeypatch):
        import app.celery_client as celery_client_module

        monkeypatch.setattr(celery_client_module.celery_client.control, "revoke", lambda *a, **kw: None)

        job_id = client.post(
            "/api/jobs", json={"url": "https://youtube.com/watch?v=abc123"}
        ).json()["job_id"]

        resp = client.post(f"/api/jobs/{job_id}/cancel")

        assert resp.status_code == 200
        data = resp.json()
        assert data["cancelled"] is False
        assert "ya había terminado" in data["detail"]

    def test_cancel_never_raises_even_if_redis_publish_fails(self, client, monkeypatch):
        # Sin Redis real en el test, el publish del evento 'cancelled'
        # falla en silencio (try/except a propósito, ver celery_client.py)
        # -- confirma que eso no tumba el endpoint.
        import app.celery_client as celery_client_module

        monkeypatch.setattr(celery_client_module.celery_client.control, "revoke", lambda *a, **kw: None)
        resp = client.post("/api/jobs/otro-job/cancel")
        assert resp.status_code == 200


class TestMapAsyncResultRevoked:
    def test_revoked_state_maps_to_cancelled_status(self):
        from unittest.mock import MagicMock

        from app.routes.jobs import map_async_result

        fake_result = MagicMock()
        fake_result.state = "REVOKED"
        fake_result.id = "job123"

        response = map_async_result(fake_result)

        assert response.status == "cancelled"
        assert response.message_key == "status.cancelled"
        assert response.job_id == "job123"
