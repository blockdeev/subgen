"""Tarea principal de Celery: orquesta el pipeline completo.

Reglas de reintento (pedidas explícitamente): backoff SOLO para fallos
transitorios de red en descarga y traducción (`TransientPipelineError`).
Todo lo demás (`DeterministicPipelineError`, `BurnError`, sin segmentos de
habla) termina la tarea en FAILURE sin reintentar, porque reintentar no va
a cambiar el resultado.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.config import get_settings
from app.pipeline.burn import BurnError, burn_subs
from app.pipeline.download import (
    DownloadError,
    TransientDownloadError,
    download_audio_only,
    download_video_full,
    has_audio_stream,
)
from app.logging_config import job_id_var
from app.pipeline.errors import DeterministicPipelineError, TransientPipelineError
from app.pipeline.progress_types import ProgressCallback, StageProgress
from app.pipeline.subtitles import make_srt, sanitize_filename
from app.pipeline.transcribe import transcribe
from app.pipeline.translate import translate
from app.progress import ProgressPublisher
from app.storage import StorageError, delete_object, list_objects_older_than, upload_file

logger = logging.getLogger(__name__)
settings = get_settings()
_publisher = ProgressPublisher(settings.redis_url)

# Progreso agregado 0-100 = (peso de la etapa) proyectado sobre el stage_pct
# 0-100 que reporta cada función del pipeline. Ver README, "decisiones de
# arquitectura", para el porqué de estos rangos.
STAGE_WEIGHTS_VIDEO = {
    "downloading": (0, 20), "transcribing": (20, 50),
    "translating": (50, 70), "burning": (70, 100),
}
STAGE_WEIGHTS_SRT = {
    "downloading": (0, 25), "transcribing": (25, 65), "translating": (65, 100),
}


def _aggregate_pct(stage: str, stage_pct: float, mode: str) -> float:
    weights = STAGE_WEIGHTS_VIDEO if mode == "video" else STAGE_WEIGHTS_SRT
    lo, hi = weights.get(stage, (0.0, 100.0))
    return lo + (hi - lo) * (max(0.0, min(stage_pct, 100.0)) / 100)


def _make_on_progress(task: Task, job_id: str, mode: str) -> ProgressCallback:
    def on_progress(event: StageProgress) -> None:
        overall = _aggregate_pct(event.stage, event.stage_pct, mode)
        payload: dict[str, Any] = {
            "job_id": job_id,
            "status": event.stage,
            "progress": round(overall, 1),
            "stage_progress": round(event.stage_pct, 1),
            "message_key": event.message_key,
            "message_params": event.message_params,
            "eta_seconds": round(event.eta_seconds) if event.eta_seconds is not None else None,
        }
        task.update_state(state="PROGRESS", meta=payload)
        _publisher.publish(job_id, payload)

    return on_progress


def _publish_terminal(job_id: str, status: str, **extra: Any) -> None:
    payload = {"job_id": job_id, "status": status, **extra}
    _publisher.publish(job_id, payload)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="app.tasks.process_video",
    acks_late=True,
    autoretry_for=(TransientPipelineError,),
    retry_backoff=settings.celery_retry_backoff,
    retry_backoff_max=settings.celery_retry_backoff_max,
    retry_jitter=True,
    max_retries=settings.celery_max_retries,
)
def process_video(self: Task, job_id: str, url: str, target_lang: str, mode: str) -> dict[str, Any]:
    """mode="srt" → solo subtítulos. mode="video" → subtítulos + video quemado."""
    job_id_var.set(job_id)  # correlación en los logs JSON de este proceso worker
    on_progress = _make_on_progress(self, job_id, mode)
    work_dir = Path(settings.work_dir) / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        video_path: Path | None = None

        if mode == "video":
            dl_result, audio_path = download_video_full(
                url, job_id, work_dir, on_progress, cookies_file=settings.ytdlp_cookies_file or None,
            )
            video_path = dl_result.path
            title, duration = dl_result.title, dl_result.duration
        else:
            dl_result = download_audio_only(
                url, job_id, work_dir, on_progress, cookies_file=settings.ytdlp_cookies_file or None,
            )
            audio_path, title, duration = dl_result.path, dl_result.title, dl_result.duration

        if settings.max_video_duration_seconds and duration > settings.max_video_duration_seconds:
            raise DeterministicPipelineError(
                f"El video dura {int(duration)}s, supera el máximo permitido "
                f"de {settings.max_video_duration_seconds}s"
            )

        if not has_audio_stream(audio_path):
            raise DeterministicPipelineError("El video no tiene pista de audio")

        segments = transcribe(
            audio_path,
            model_name=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            beam_size=settings.whisper_beam_size,
            vad_min_silence_ms=settings.whisper_vad_min_silence_ms,
            vad_speech_pad_ms=settings.whisper_vad_speech_pad_ms,
            audio_duration_seconds=duration,
            on_progress=on_progress,
        )
        if not segments:
            raise DeterministicPipelineError("No se detectó habla en el audio")

        translated = translate(
            segments,
            target_lang=target_lang,
            batch_size=settings.translate_batch_size,
            on_progress=on_progress,
        )

        safe_title = sanitize_filename(title)
        srt_name = f"{safe_title}_{target_lang}.srt"
        srt_path = make_srt(translated, work_dir / srt_name)

        srt_key = f"{job_id}/{srt_name}"
        upload_file(settings, srt_path, srt_key)

        result: dict[str, Any] = {
            "title": title,
            "duration": duration,
            "segments_count": len(translated),
            "srt_key": srt_key,
            "srt_filename": srt_name,
            "mode": mode,
            "preview_segments": [
                {"start": s.start, "end": s.end, "text": s.text, "text_original": s.text_original}
                for s in translated[:5]
            ],
        }

        if mode == "video" and video_path is not None and video_path.exists():
            burned_name = f"{safe_title}_subtitulado.mp4"
            burned_path = work_dir / burned_name
            burn_subs(video_path, srt_path, burned_path, settings=settings, on_progress=on_progress)

            video_key = f"{job_id}/{burned_name}"
            upload_file(settings, burned_path, video_key)
            result["video_key"] = video_key
            result["video_filename"] = burned_name
            result["video_size_mb"] = round(burned_path.stat().st_size / (1024 * 1024), 1)

        _publish_terminal(job_id, "completed", progress=100, result=result)
        return result

    except SoftTimeLimitExceeded:
        logger.error("Job %s superó el soft_time_limit", job_id)
        _publish_terminal(job_id, "error", message_key="status.error_timeout")
        raise

    except DownloadError as exc:
        # OJO: DownloadError es subclase de DeterministicPipelineError, así
        # que este except tiene que ir ANTES que el de DeterministicPipelineError
        # o nunca se alcanza.
        logger.warning("Job %s: error de descarga, no se reintenta: %s", job_id, exc)
        _publish_terminal(job_id, "error", message_key="status.error_download",
                           message_params={"detail": str(exc)})
        raise

    except DeterministicPipelineError as exc:
        logger.warning("Job %s: error determinístico, no se reintenta: %s", job_id, exc)
        _publish_terminal(job_id, "error", message_key="status.error_generic",
                           message_params={"detail": str(exc)})
        raise

    except BurnError as exc:
        logger.error("Job %s: error de FFmpeg, no se reintenta: %s", job_id, exc)
        _publish_terminal(job_id, "error", message_key="status.error_burn",
                           message_params={"detail": str(exc)})
        raise

    except StorageError as exc:
        logger.error("Job %s: error subiendo resultado a storage: %s", job_id, exc)
        _publish_terminal(job_id, "error", message_key="status.error_storage",
                           message_params={"detail": str(exc)})
        raise

    except (TransientDownloadError, TransientPipelineError) as exc:
        # OJO: acá NO alcanza con "loguear y dejar que autoretry reintente".
        # Cada reintento de Celery vuelve a ejecutar la tarea COMPLETA desde
        # el principio (no hay checkpointing intra-tarea), así que este
        # except corre una vez por intento. Si es el ÚLTIMO intento
        # permitido, `autoretry_for` NO va a reintentar de nuevo — va a
        # dejar que la excepción se propague y termine la tarea en FAILURE.
        # Sin este chequeo, esa falla final nunca le llega al frontend (no
        # se publica ningún evento) y la UI se queda esperando para
        # siempre, como si el pipeline siguiera trabajando en silencio.
        retries_agotados = self.request.retries >= (self.max_retries or 0)
        if retries_agotados:
            logger.error("Job %s: fallo transitorio, se agotaron los reintentos (%d/%d)",
                          job_id, self.request.retries, self.max_retries)
            _publish_terminal(job_id, "error", message_key="status.error_generic",
                               message_params={"detail": str(exc)})
        else:
            logger.warning("Job %s: fallo transitorio, Celery va a reintentar (intento %d/%d)",
                            job_id, self.request.retries + 1, self.max_retries)
        raise

    except Exception as exc:  # noqa: BLE001 - red de seguridad, ver docstring
        # Cualquier excepción que no matcheó ninguno de los except de arriba
        # (p.ej. un IndexError de una librería de audio con un archivo
        # corrupto, un crash inesperado de FFmpeg, lo que sea) tiene que
        # terminar avisándole al frontend igual. Si esto no estuviera acá,
        # la tarea termina en FAILURE del lado de Celery pero el usuario se
        # queda mirando la barra de progreso congelada para siempre, porque
        # nunca se publicó un evento terminal por WebSocket/Redis. Pasó de
        # verdad con un IndexError de PyAV al intentar transcribir un video
        # sin pista de audio que además hizo fallar la extracción previa.
        logger.exception("Job %s: excepción no prevista", job_id)
        _publish_terminal(job_id, "error", message_key="status.error_generic",
                           message_params={"detail": str(exc)})
        raise

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@celery_app.task(name="app.tasks.cleanup_expired_outputs")  # type: ignore[untyped-decorator]
def cleanup_expired_outputs() -> int:
    """Tarea periódica (Celery Beat, embebida en worker-1 vía `-B`).

    Borra del bucket los objetos más viejos que `cleanup_max_age_hours`.
    """
    stale_keys = list_objects_older_than(settings, prefix="", max_age_hours=settings.cleanup_max_age_hours)
    for key in stale_keys:
        delete_object(settings, key)
    if stale_keys:
        logger.info("Limpieza: %d objetos borrados del bucket", len(stale_keys))
    return len(stale_keys)
