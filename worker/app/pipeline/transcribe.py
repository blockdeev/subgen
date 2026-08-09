"""Transcripción con faster-whisper.

Parámetros del modelo (beam_size, vad_filter, vad_parameters, language,
task) migrados TAL CUAL desde app.py. Se agrega progreso real comparando el
timestamp del último segmento emitido contra la duración total del audio,
en vez del conteo heurístico de segmentos que tenía el original.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from app.pipeline.progress_types import ProgressCallback, StageProgress, noop_progress

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

_model: "Optional[WhisperModel]" = None
_model_lock = threading.Lock()


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


def get_whisper_model(model_name: str, device: str, compute_type: str) -> "WhisperModel":
    """Singleton por proceso worker (igual que el original, mismo motivo:
    cargar el modelo es costoso y un worker corre con concurrency=1)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel

                logger.info("Cargando modelo Whisper '%s' (device=%s, compute_type=%s)",
                            model_name, device, compute_type)
                _model = WhisperModel(model_name, device=device, compute_type=compute_type)
                logger.info("Modelo Whisper cargado")
    return _model


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
    on_progress: ProgressCallback = noop_progress,
) -> list[Segment]:
    on_progress(StageProgress(stage="transcribing", stage_pct=0.0,
                               message_key="status.loading_model"))
    model = get_whisper_model(model_name, device, compute_type)

    on_progress(StageProgress(stage="transcribing", stage_pct=2.0,
                               message_key="status.transcribing"))

    segs_raw, info = model.transcribe(
        str(audio_path),
        language="en",
        task="transcribe",
        beam_size=beam_size,
        word_timestamps=False,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=vad_min_silence_ms,
            speech_pad_ms=vad_speech_pad_ms,
        ),
    )

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
