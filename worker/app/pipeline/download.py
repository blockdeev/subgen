"""Descarga de audio/video con yt-dlp.

Lógica de negocio (format selectors, postprocesadores) migrada TAL CUAL desde
la app.py original. Lo único nuevo es progreso real vía progress_hooks en vez
de porcentajes fijos, y tipado completo.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from app.pipeline.errors import DeterministicPipelineError, TransientPipelineError
from app.pipeline.progress_types import ProgressCallback, StageProgress, noop_progress

logger = logging.getLogger(__name__)


class DownloadError(DeterministicPipelineError):
    """Fallo determinístico de descarga (URL inválida, sin formatos, etc.).

    No debe reintentarse automáticamente: reintentar una URL inválida no la
    va a volver válida.
    """


class TransientDownloadError(TransientPipelineError):
    """Fallo de red transitorio. Sí conviene reintentar con backoff."""


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    title: str
    duration: float  # segundos, 0 si yt-dlp no lo reporta


def has_audio_stream(path: Path) -> bool:
    """True si el archivo tiene al menos un stream de audio, chequeado con
    ffprobe. Usado para detectar "video sin pista de audio" ANTES de
    pasárselo a Whisper — que si no, explota con un IndexError feo de PyAV
    en vez de un error claro para el usuario."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ffprobe timeout chequeando streams de audio de %s", path.name)
        return True  # no bloqueamos el pipeline por un timeout de ffprobe
    return bool(result.stdout.strip())


def _make_hook(job_id: str, on_progress: ProgressCallback) -> Callable[[dict[str, Any]], None]:
    def hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            pct = (downloaded / total * 100) if total else 0.0
            eta = d.get("eta")
            on_progress(
                StageProgress(
                    stage="downloading",
                    stage_pct=min(pct, 99.0),
                    message_key="status.downloading",
                    message_params={"percent": round(pct, 1)},
                    eta_seconds=float(eta) if eta is not None else None,
                )
            )
        elif d.get("status") == "finished":
            on_progress(
                StageProgress(
                    stage="downloading",
                    stage_pct=100.0,
                    message_key="status.downloading_finished",
                )
            )

    return hook


def _classify_and_raise(exc: Exception) -> None:
    """Traduce excepciones de yt-dlp en DownloadError vs TransientDownloadError."""
    import yt_dlp

    if isinstance(exc, yt_dlp.utils.DownloadError):
        msg = str(exc).lower()
        rate_limit_markers = ("429", "too many requests")
        # El extractor de lbry/Odysee pierde el detalle del 429 en el
        # mensaje final -- queda solo "No video formats found!" (confirmado
        # en vivo: el 429 real aparece en un WARNING intermedio -- "Unable
        # to download webpage: HTTP Error 429" -- que se descarta antes de
        # llegar a esta excepción). Lo detectamos igual por este patrón
        # puntual del extractor. Peor caso si el patrón da falso positivo:
        # 3 reintentos de más antes de fallar igual (celery_max_retries),
        # no hay riesgo de loop infinito.
        is_lbry_hidden_rate_limit = "[lbry]" in msg and "no video formats found" in msg
        if any(m in msg for m in rate_limit_markers) or is_lbry_hidden_rate_limit:
            raise TransientDownloadError(
                "La plataforma de origen está limitando las descargas en "
                "este momento. Reintentando automáticamente."
            ) from exc

        transient_markers = ("timed out", "timeout", "connection reset", "temporary failure",
                              "503", "502", "504", "network")
        if any(m in msg for m in transient_markers):
            raise TransientDownloadError(str(exc)) from exc
        raise DownloadError(str(exc)) from exc
    raise TransientDownloadError(str(exc)) from exc


def _maybe_add_cookies(
    ydl_opts: dict[str, Any], cookies_file: Optional[str], writable_dir: Path
) -> dict[str, Any]:
    """Agrega `cookiefile` a ydl_opts si `cookies_file` apunta a un archivo
    real y no vacío — pero SIEMPRE una COPIA en `writable_dir`, nunca el
    original.

    yt-dlp reescribe el cookiejar al terminar (persiste cookies de sesión
    actualizadas). El archivo original se monta `:ro` a propósito (para no
    pisar el cookies.txt del host) — pasárselo directo a yt-dlp explota con
    "Read-only file system", y esa excepción no es la de yt-dlp así que
    `_classify_and_raise` la trata como transitoria por default, reiniciando
    el pipeline entero en bucle. Copiando a un lugar escribible se evita el
    problema de raíz sin perder la protección read-only del mount original.
    """
    if cookies_file and Path(cookies_file).is_file() and Path(cookies_file).stat().st_size > 0:
        local_copy = writable_dir / "cookies.txt"
        shutil.copy(cookies_file, local_copy)
        ydl_opts["cookiefile"] = str(local_copy)
    return ydl_opts


def _maybe_add_proxy(ydl_opts: dict[str, Any], proxy: Optional[str]) -> dict[str, Any]:
    """Agrega `proxy` a ydl_opts si viene seteado y no vacío.

    Vacío/None se omite a propósito en vez de pasar "" — yt-dlp interpreta
    la cadena vacía como "ignorar cualquier proxy del entorno", que no es
    lo mismo que "no configurar nada". Ver `ytdlp_proxy` en config.py para
    el porqué de esta opción (reputación de IP de datacenter).
    """
    if proxy:
        ydl_opts["proxy"] = proxy
    return ydl_opts


def download_audio_only(
    url: str,
    job_id: str,
    downloads_dir: Path,
    on_progress: ProgressCallback = noop_progress,
    cookies_file: Optional[str] = None,
    proxy: Optional[str] = None,
) -> DownloadResult:
    """Modo rápido: solo descarga el audio. Mismos ydl_opts que el original."""
    import yt_dlp

    ydl_opts = _maybe_add_proxy(_maybe_add_cookies({
        "format": "bestaudio/best",
        "outtmpl": str(downloads_dir / job_id) + ".%(ext)s",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [_make_hook(job_id, on_progress)],
        # Sin esto, YouTube puede servir un set reducido de formatos (el
        # PoToken de verificación de origen queda sin resolver) -- se
        # manifiesta como "Requested format is not available", confirmado
        # en un deploy real desde IP de datacenter (mucho más estricto que
        # desde una IP residencial). "ejs:github" baja el script solucionador
        # del propio repo de yt-dlp, no de un tercero. Necesita además un
        # runtime de JS instalado (deno, ver Dockerfile) -- las dos cosas
        # juntas son las que arreglan esto, ninguna sola alcanza.
        "remote_components": ["ejs:github"],
    }, cookies_file, downloads_dir), proxy)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001 - reclasificamos explícitamente abajo
        _classify_and_raise(exc)
        raise  # inalcanzable, _classify_and_raise siempre lanza

    title = info.get("title", "video")
    duration = float(info.get("duration") or 0)

    audio = downloads_dir / f"{job_id}.mp3"
    if not audio.exists():
        for f in downloads_dir.glob(f"{job_id}.*"):
            if f.suffix in (".mp3", ".m4a", ".wav", ".opus", ".webm", ".ogg"):
                audio = f
                break
    if not audio.exists():
        raise DownloadError("No se encontró el audio descargado")

    return DownloadResult(path=audio, title=title, duration=duration)


def download_video_full(
    url: str,
    job_id: str,
    downloads_dir: Path,
    on_progress: ProgressCallback = noop_progress,
    cookies_file: Optional[str] = None,
    proxy: Optional[str] = None,
) -> tuple[DownloadResult, Path]:
    """Modo completo: descarga video + extrae audio. Devuelve (video, audio_path)."""
    import yt_dlp

    ydl_opts = _maybe_add_proxy(_maybe_add_cookies({
        "format": "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "outtmpl": str(downloads_dir / job_id) + ".%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [_make_hook(job_id, on_progress)],
        "remote_components": ["ejs:github"],  # ver comentario en download_audio_only
    }, cookies_file, downloads_dir), proxy)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001
        _classify_and_raise(exc)
        raise

    title = info.get("title", "video")
    duration = float(info.get("duration") or 0)

    video = downloads_dir / f"{job_id}.mp4"
    if not video.exists():
        for f in downloads_dir.glob(f"{job_id}.*"):
            if f.suffix in (".mp4", ".mkv", ".webm"):
                video = f
                break
    if not video.exists():
        raise DownloadError("No se encontró el video descargado")

    # Extraer audio (igual que el original; si falla, Whisper lee el video directo)
    audio = downloads_dir / f"{job_id}_audio.mp3"
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(video), "-vn", "-acodec", "libmp3lame",
             "-q:a", "2", "-y", str(audio)],
            capture_output=True, timeout=300, check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "ffmpeg no pudo extraer audio de %s (código %s); se usará el video "
                "directamente como fuente de audio para Whisper. stderr: %s",
                video.name, result.returncode, result.stderr[-300:].decode(errors="replace"),
            )
    except subprocess.TimeoutExpired:
        logger.warning("Timeout extrayendo audio de %s; se usa el video directo", video.name)

    audio_path = audio if audio.exists() else video

    return DownloadResult(path=video, title=title, duration=duration), audio_path
