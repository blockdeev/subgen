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
    # cpu_threads=0 (el default de faster-whisper) NO garantiza usar todos
    # los cores disponibles -- ctranslate2 lee OMP_NUM_THREADS si está
    # seteada, y si no, cae al default del runtime de OpenMP, que varía
    # (documentado como fuente de confusión real en la comunidad). 0 acá
    # significa "detectar cores disponibles" (os.cpu_count()), no "dejarlo
    # en manos de OpenMP".
    whisper_cpu_threads: int = Field(default=0)
    # BatchedInferencePipeline (faster-whisper >=1.0): procesa chunks en
    # paralelo en vez de secuencial. Detrás de un flag, default OFF --
    # sin verificar en hardware real que no degrada timestamps/calidad
    # (ver README, nota de esta feature).
    whisper_use_batched: bool = Field(default=False)
    whisper_batch_size: int = Field(default=8)
    whisper_vad_min_silence_ms: int = Field(default=500)
    whisper_vad_speech_pad_ms: int = Field(default=200)

    # Traducción
    translate_batch_size: int = Field(default=25)
    # Lotes en paralelo, no secuenciales -- era ~15% del job entero en
    # pura latencia de red esperando uno por uno. 4-6 es razonable sin
    # gatillar rate-limiting agresivo de Google; cada lote individual ya
    # tiene su propio backoff si lo pega igual (ver translate.py).
    translate_concurrency: int = Field(default=2)
    # Mitigación (no arregla la causa raíz, ver ARCHITECTURE.md "Problemas
    # conocidos"): con concurrency=1, esto fuerza un proceso NUEVO del pool
    # de Celery cada N tareas -- cualquier thread huérfano de ctranslate2
    # o proceso de FFmpeg que haya quedado vivo dentro del proceso viejo
    # muere junto con él. 1 = proceso nuevo en cada job (máxima protección,
    # recarga el modelo Whisper cada vez, ~1.5s medido -- despreciable
    # contra jobs de decenas de minutos). 0 o negativo = deshabilitado
    # (comportamiento viejo, sin esta protección).
    celery_max_tasks_per_child: int = Field(default=1)
    # Chequeo pre-vuelo antes de arrancar un job: si no hay al menos esto
    # de espacio libre en el disco de work_dir, falla YA en vez de a mitad
    # de un burn largo. ~500MB de source + ~1GB de output por job (según
    # medición real) -- 3GB da margen para eso más limpieza pendiente.
    # <= 0 deshabilita el chequeo.
    min_free_disk_mb: int = Field(default=3072)

    # Límites de negocio
    max_video_duration_seconds: int = Field(default=3600, description="0 = sin límite")

    # Cookies de sesión para yt-dlp (opcional). Necesario cuando YouTube
    # bloquea la descarga con "Sign in to confirm you're not a bot" — cada
    # vez más común sin cookies, sobre todo desde IPs de datacenter/VPS.
    # Formato Netscape (cookies.txt). Ver README, sección "Cookies de
    # YouTube". Vacío = no se pasa ningún cookiefile a yt-dlp (default).
    ytdlp_cookies_file: str = Field(default="")

    # Proxy de salida para yt-dlp (opcional). Existe porque las IPs de
    # datacenter (Hetzner, DigitalOcean, etc.) están mal vistas por las
    # plataformas de video: YouTube pide cookies constantemente y el CDN
    # de Odysee (CDN77) devuelve 429 por reputación de rango, no por
    # volumen real de pedidos. Verificado en producción: el MISMO video
    # que falla desde la VPS anda perfecto desde una IP residencial.
    #
    # Con Cloudflare WARP como salida, YouTube descarga sin cookies
    # (confirmado en vivo) y CDN77 deja de tirar 429. Ver DEPLOYMENT.md,
    # sección "Proxy de salida (Cloudflare WARP)".
    #
    # Formato: "socks5h://IP:PUERTO" (socks5h = el proxy resuelve el DNS).
    # Vacío = sin proxy, salida directa por la IP de la VPS (default).
    ytdlp_proxy: str = Field(default="")

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
