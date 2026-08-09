"""Quemado de subtítulos con FFmpeg.

El estilo (`FontName=Arial,FontSize=20,...`), los flags de codificación
(`libx264`, `preset fast`, `crf 23`, audio copiado) y el fallback sin
`force_style` son los mismos del original — vienen de `WorkerSettings` con
esos valores como default, así que el comportamiento no cambia.

Lo nuevo: `ffprobe` para la duración total + lectura no bloqueante de
`-progress pipe:1 -nostats`, parseo de `out_time_ms`, y ETA estimado con la
`speed` que reporta FFmpeg (con fallback a velocidad observada
tiempo-transcurrido / progreso si FFmpeg no reporta `speed` en ese bloque).
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from app.pipeline.progress_types import ProgressCallback, StageProgress, noop_progress

logger = logging.getLogger(__name__)


class BurnError(RuntimeError):
    """FFmpeg falló (con y sin force_style) o no generó un archivo válido."""


def probe_duration_seconds(video_path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        logger.warning("ffprobe no pudo determinar la duración de %s", video_path.name)
        return 0.0


def _escape_srt_path(srt_path: Path) -> str:
    return str(srt_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _build_style(settings: Any) -> str:
    return (
        f"FontName={settings.burn_font_name},FontSize={settings.burn_font_size},"
        f"PrimaryColour={settings.burn_primary_colour},"
        f"OutlineColour={settings.burn_outline_colour},"
        f"BorderStyle={settings.burn_border_style},Outline={settings.burn_outline},"
        f"Shadow={settings.burn_shadow},MarginV={settings.burn_margin_v}"
    )


def iter_progress_blocks(lines: Iterable[str]) -> Iterable[dict[str, str]]:
    """Agrupa las líneas `clave=valor` de `-progress pipe:1` en bloques.

    FFmpeg emite un bloque de líneas terminado en `progress=continue` (o
    `progress=end`) por cada intervalo de progreso. Función pura: no toca
    subprocess ni I/O, así que es directamente testeable con una lista de
    strings de ejemplo.
    """
    block: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        block[key] = value
        if key == "progress":
            yield block
            block = {}


def compute_progress(
    block: dict[str, str],
    *,
    total_seconds: float,
    elapsed_seconds: float,
) -> StageProgress | None:
    """Convierte un bloque de progreso de FFmpeg ya parseado en un StageProgress.

    Función pura, sin efectos secundarios: fácil de testear con bloques
    construidos a mano. Devuelve None si el bloque no trae `out_time_ms` o
    no hay duración total conocida (no se puede calcular porcentaje).
    """
    out_time_ms = block.get("out_time_ms")
    if not out_time_ms or total_seconds <= 0:
        return None

    try:
        processed_seconds = int(out_time_ms) / 1_000_000
    except ValueError:
        return None

    pct = min(max(processed_seconds / total_seconds * 100, 0.0), 99.9)

    speed_raw = block.get("speed", "").rstrip("x").strip()
    try:
        speed = float(speed_raw) if speed_raw else 0.0
    except ValueError:
        speed = 0.0

    eta: float | None = None
    if speed > 0:
        eta = max(total_seconds - processed_seconds, 0) / speed
    elif processed_seconds > 0 and elapsed_seconds > 0:
        rate = processed_seconds / elapsed_seconds
        if rate > 0:
            eta = max(total_seconds - processed_seconds, 0) / rate

    return StageProgress(
        stage="burning",
        stage_pct=pct,
        message_key="status.burning_progress",
        message_params={
            "percent": round(pct, 1),
            "processed_seconds": round(processed_seconds),
            "total_seconds": round(total_seconds),
        },
        eta_seconds=eta,
    )


def _parse_progress_stream(
    process: "subprocess.Popen[str]",
    total_seconds: float,
    on_progress: ProgressCallback,
    started_at: float,
) -> None:
    """Lee stdout de `-progress pipe:1` en un thread separado (para no
    bloquear el proceso principal mientras FFmpeg corre) y publica progreso
    usando las funciones puras de arriba."""
    assert process.stdout is not None
    for block in iter_progress_blocks(iter(process.stdout.readline, "")):
        elapsed = time.monotonic() - started_at
        event = compute_progress(block, total_seconds=total_seconds, elapsed_seconds=elapsed)
        if event is not None:
            on_progress(event)


def _run_ffmpeg_with_progress(
    cmd: list[str],
    total_seconds: float,
    timeout_seconds: int,
    on_progress: ProgressCallback,
) -> tuple[int, str]:
    started_at = time.monotonic()
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )

    reader = threading.Thread(
        target=_parse_progress_stream, args=(process, total_seconds, on_progress, started_at),
        daemon=True,
    )
    reader.start()

    try:
        _, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise BurnError(f"FFmpeg superó el timeout de {timeout_seconds}s")
    finally:
        reader.join(timeout=5)

    return process.returncode, stderr or ""


def burn_subs(
    video_path: Path,
    srt_path: Path,
    output_path: Path,
    *,
    settings: Any,
    on_progress: ProgressCallback = noop_progress,
) -> Path:
    total_seconds = probe_duration_seconds(video_path)
    on_progress(StageProgress(stage="burning", stage_pct=0.0, message_key="status.burning_start"))

    srt_esc = _escape_srt_path(srt_path)
    style = _build_style(settings)

    base_cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"subtitles={srt_esc}:force_style='{style}'",
        "-c:v", "libx264", "-preset", settings.ffmpeg_preset, "-crf", str(settings.ffmpeg_crf),
        "-c:a", "copy", "-y",
        "-progress", "pipe:1", "-nostats",
        str(output_path),
    ]

    logger.info("FFmpeg: quemando subs en %s", video_path.name)
    returncode, stderr = _run_ffmpeg_with_progress(
        base_cmd, total_seconds, settings.ffmpeg_timeout_seconds, on_progress
    )

    if returncode != 0:
        logger.warning("FFmpeg con force_style falló, reintentando sin él")
        fallback_cmd = [
            "ffmpeg", "-i", str(video_path),
            "-vf", f"subtitles={srt_esc}",
            "-c:v", "libx264", "-preset", settings.ffmpeg_preset, "-crf", str(settings.ffmpeg_crf),
            "-c:a", "copy", "-y",
            "-progress", "pipe:1", "-nostats",
            str(output_path),
        ]
        returncode, stderr = _run_ffmpeg_with_progress(
            fallback_cmd, total_seconds, settings.ffmpeg_timeout_seconds, on_progress
        )
        if returncode != 0:
            logger.error("FFmpeg error: %s", stderr[-500:])
            raise BurnError(f"FFmpeg falló: {stderr[-200:]}")

    if not output_path.exists() or output_path.stat().st_size < 1000:
        raise BurnError("FFmpeg no generó un archivo de salida válido")

    on_progress(StageProgress(stage="burning", stage_pct=100.0, message_key="status.burning_done"))
    return output_path
