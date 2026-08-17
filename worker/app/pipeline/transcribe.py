"""Transcripción con faster-whisper.

Parámetros del modelo (beam_size, vad_filter, vad_parameters, language,
task) migrados TAL CUAL desde app.py. Se agrega progreso real comparando el
timestamp del último segmento emitido contra la duración total del audio,
en vez del conteo heurístico de segmentos que tenía el original.

Dos cosas nuevas, las dos detrás de configuración explícita:

- `cpu_threads`: sin pasarlo, `WhisperModel` usa `cpu_threads=0`, que NO
  garantiza usar todos los cores disponibles (ctranslate2 lee
  OMP_NUM_THREADS si está seteada, si no cae al default del runtime de
  OpenMP -- documentado como fuente de confusión real en la comunidad de
  faster-whisper). Acá se resuelve explícito a los cores que ve el
  contenedor.

- `BatchedInferencePipeline`: detrás de `use_batched` (default False, sin
  verificar en hardware real). OJO con un detalle que no es obvio:
  `BatchedInferencePipeline.transcribe()` tiene `without_timestamps=True`
  por default (a diferencia de `WhisperModel.transcribe()`, que lo tiene
  en `False`) -- si no se pisa explícito, el modo batched devolvería
  segmentos SIN timestamps utilizables, rompiendo el pipeline entero en
  silencio. Se fuerza `without_timestamps=False` siempre, en los dos
  caminos, sin importar el default de la librería.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from app.pipeline.progress_types import ProgressCallback, StageProgress, noop_progress

if TYPE_CHECKING:
    from faster_whisper import BatchedInferencePipeline, WhisperModel

logger = logging.getLogger(__name__)

_model: "Optional[WhisperModel]" = None
_batched_pipeline: "Optional[BatchedInferencePipeline]" = None
_model_lock = threading.Lock()


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


def resolve_cpu_threads(configured: int) -> int:
    """`configured <= 0` -> cores que ve ESTE contenedor (no el host, si
    hay un cgroup de por medio). `sched_getaffinity` es cgroup-aware en
    Linux; `os.cpu_count()` como fallback en plataformas donde no existe.
    """
    if configured > 0:
        return configured
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 4


def get_whisper_model(model_name: str, device: str, compute_type: str, cpu_threads: int = 0) -> "WhisperModel":
    """Singleton por proceso worker (igual que el original, mismo motivo:
    cargar el modelo es costoso y un worker corre con concurrency=1)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel

                effective_threads = resolve_cpu_threads(cpu_threads)
                logger.info(
                    "Cargando modelo Whisper '%s' (device=%s, compute_type=%s, cpu_threads=%d)",
                    model_name, device, compute_type, effective_threads,
                )
                _model = WhisperModel(
                    model_name, device=device, compute_type=compute_type, cpu_threads=effective_threads,
                )
                logger.info("Modelo Whisper cargado")
    return _model


def get_batched_pipeline(model_name: str, device: str, compute_type: str, cpu_threads: int = 0) -> "BatchedInferencePipeline":
    """Envuelve el mismo singleton de `get_whisper_model` -- no se carga
    el modelo dos veces.

    OJO: `get_whisper_model()` se resuelve ANTES de tomar `_model_lock`
    acá abajo -- tiene su propio locking interno, y si se llamara desde
    adentro de este `with _model_lock:` sería un deadlock real (mismo
    `threading.Lock`, no reentrante, el mismo thread esperando por un
    lock que él mismo ya tiene tomado). Esto lo encontré así, corriendo
    los tests reales, no solo por inspección de código.
    """
    global _batched_pipeline
    if _batched_pipeline is None:
        model = get_whisper_model(model_name, device, compute_type, cpu_threads)
        with _model_lock:
            if _batched_pipeline is None:
                from faster_whisper import BatchedInferencePipeline

                _batched_pipeline = BatchedInferencePipeline(model=model)
                logger.info("BatchedInferencePipeline armado sobre el modelo ya cargado")
    return _batched_pipeline


def transcribe(
    audio_path: Path,
    *,
    model_name: str,
    device: str,
    compute_type: str,
    beam_size: int = 5,
    vad_min_silence_ms: int = 500,
    vad_speech_pad_ms: int = 200,
    audio_duration_seconds: float = 0.0,
    cpu_threads: int = 0,
    use_batched: bool = False,
    batch_size: int = 8,
    on_progress: ProgressCallback = noop_progress,
) -> list[Segment]:
    on_progress(StageProgress(stage="transcribing", stage_pct=0.0,
                               message_key="status.loading_model"))

    common_kwargs = dict(
        language="en",
        task="transcribe",
        beam_size=beam_size,
        word_timestamps=False,
        without_timestamps=False,  # ver docstring del módulo -- nunca confiar en el default de la lib
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=vad_min_silence_ms,
            speech_pad_ms=vad_speech_pad_ms,
        ),
    )

    on_progress(StageProgress(stage="transcribing", stage_pct=2.0,
                               message_key="status.transcribing"))

    if use_batched:
        pipeline = get_batched_pipeline(model_name, device, compute_type, cpu_threads)
        segs_raw, info = pipeline.transcribe(str(audio_path), batch_size=batch_size, **common_kwargs)
    else:
        model = get_whisper_model(model_name, device, compute_type, cpu_threads)
        segs_raw, info = model.transcribe(str(audio_path), **common_kwargs)

    segments: list[Segment] = []
    for raw in segs_raw:
        seg = Segment(start=raw.start, end=raw.end, text=raw.text.strip())
        segments.append(seg)

        if audio_duration_seconds > 0:
            pct = min(seg.end / audio_duration_seconds * 100, 99.0)
        else:
            # Sin duración conocida, caemos al heurístico del original
            pct = min(2.0 + len(segments) * 0.5, 99.0)

        if len(segments) % 5 == 0:
            on_progress(
                StageProgress(
                    stage="transcribing",
                    stage_pct=pct,
                    message_key="status.transcribing_progress",
                    message_params={"count": len(segments)},
                )
            )

    on_progress(StageProgress(stage="transcribing", stage_pct=100.0,
                               message_key="status.transcribing_done",
                               message_params={"count": len(segments)}))
    logger.info("Transcripción: %d segmentos, idioma detectado: %s", len(segments), info.language)
    return segments
