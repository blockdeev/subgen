"""Configuración del worker, cargada desde variables de entorno."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SUBGEN_", extra="ignore")

    # Redis / Celery
    redis_url: str = Field(default="redis://localhost:6379/0")
    celery_task_time_limit: int = Field(default=1800, description="Hard limit en segundos")
    celery_task_soft_time_limit: int = Field(default=1700, description="Soft limit en segundos")
    celery_max_retries: int = Field(default=3)
    celery_retry_backoff: int = Field(default=10, description="Backoff base en segundos")
    celery_retry_backoff_max: int = Field(default=120)
    worker_concurrency: int = Field(default=1, description="Jobs simultáneos por worker")

    # Whisper / faster-whisper
    whisper_model: str = Field(default="small")
    whisper_device: Literal["cpu", "cuda", "auto"] = Field(default="cpu")
    whisper_compute_type: str = Field(default="int8")
    whisper_beam_size: int = Field(default=5)
    whisper_vad_min_silence_ms: int = Field(default=500)
    whisper_vad_speech_pad_ms: int = Field(default=200)

    # Traducción
    translate_batch_size: int = Field(default=25)
    # Lotes en paralelo, no secuenciales -- era ~15% del job entero en
    # pura latencia de red esperando uno por uno. 4-6 es razonable sin
    # gatillar rate-limiting agresivo de Google; cada lote individual ya
    # tiene su propio backoff si lo pega igual (ver translate.py).
    translate_concurrency: int = Field(default=4)

    # Límites de negocio
    max_video_duration_seconds: int = Field(default=3600, description="0 = sin límite")

    # Cookies de sesión para yt-dlp (opcional). Necesario cuando YouTube
    # bloquea la descarga con "Sign in to confirm you're not a bot" — cada
    # vez más común sin cookies, sobre todo desde IPs de datacenter/VPS.
    # Formato Netscape (cookies.txt). Ver README, sección "Cookies de
    # YouTube". Vacío = no se pasa ningún cookiefile a yt-dlp (default).
    ytdlp_cookies_file: str = Field(default="")

    # FFmpeg / quemado de subtítulos (estilo validado, NO cambiar)
    burn_font_name: str = Field(default="Arial")
    burn_font_size: int = Field(default=20)
    burn_primary_colour: str = Field(default="&HFFFFFF&")
    burn_outline_colour: str = Field(default="&H000000&")
    burn_border_style: int = Field(default=1)
    burn_outline: int = Field(default=2)
    burn_shadow: int = Field(default=1)
    burn_margin_v: int = Field(default=25)
    ffmpeg_preset: str = Field(default="fast")
    ffmpeg_crf: int = Field(default=23)
    # Default de frame rate de salida al quemar subtítulos (si el request
    # no especifica uno). 30fps es casi indistinguible de 60fps para una
    # charla hablada y reduce a la mitad el trabajo tanto de libass (que es
    # mono-hilo) como de x264. 0 = no tocar el frame rate nativo del source.
    default_burn_fps: int = Field(default=30)
    ffmpeg_timeout_seconds: int = Field(default=1800)

    # Almacenamiento S3-compatible (ver README, sección "Almacenamiento")
    s3_endpoint_url: str = Field(default="http://minio:9000")
    s3_access_key: str = Field(default="subgen")
    s3_secret_key: str = Field(default="subgen12345")
    s3_bucket: str = Field(default="subgen-outputs")
    s3_region: str = Field(default="us-east-1")
    s3_use_ssl: bool = Field(default=False)
    s3_presigned_url_expiry_seconds: int = Field(default=3600)

    # Almacenamiento local temporal (dentro del propio contenedor worker)
    work_dir: str = Field(default="/tmp/subgen")

    # Limpieza automática (Celery Beat)
    cleanup_max_age_hours: int = Field(default=24)
    cleanup_interval_seconds: int = Field(default=3600)

    # Logging
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
