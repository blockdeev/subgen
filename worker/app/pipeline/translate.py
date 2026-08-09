"""Traducción de segmentos con deep-translator (GoogleTranslator).

Lógica de negocio TAL CUAL el original: lotes de 25, y si falla el lote
completo se reintenta ítem por ítem, y si un ítem individual falla se
conserva el texto en inglés (degradación silenciosa intencional, no la
tocamos). Lo nuevo es tipado, progreso por stage_pct, y dejar que un fallo
en la construcción del traductor (p.ej. sin red) se propague para que la
tarea Celery decida si reintenta.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.pipeline.errors import TransientPipelineError
from app.pipeline.progress_types import ProgressCallback, StageProgress, noop_progress
from app.pipeline.transcribe import Segment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranslatedSegment:
    start: float
    end: float
    text_original: str
    text: str


def translate(
    segments: list[Segment],
    *,
    target_lang: str = "es",
    batch_size: int = 25,
    on_progress: ProgressCallback = noop_progress,
) -> list[TranslatedSegment]:
    from deep_translator import GoogleTranslator

    on_progress(StageProgress(stage="translating", stage_pct=0.0, message_key="status.translating"))

    try:
        translator = GoogleTranslator(source="en", target=target_lang)
    except Exception as exc:  # noqa: BLE001 - no controlamos las excepciones de la lib externa
        # Instanciar el traductor solo falla por problemas de red/DNS/servicio
        # caído: es el único punto de translate() que consideramos transitorio
        # y reintentable. El resto (fallos por lote/ítem) se degrada en
        # silencio más abajo, igual que en el original.
        raise TransientPipelineError(f"No se pudo inicializar el traductor: {exc}") from exc
    out: list[TranslatedSegment] = []
    total = len(segments)
    if total == 0:
        return out

    for i in range(0, total, batch_size):
        batch = segments[i : i + batch_size]
        texts = [s.text for s in batch]
        try:
            results = translator.translate_batch(texts)
        except Exception:
            logger.warning("Fallo el lote de traducción [%d:%d], reintentando ítem por ítem", i, i + batch_size)
            results = []
            for t in texts:
                try:
                    results.append(translator.translate(t))
                except Exception:
                    results.append(t)  # degradación intencional: se conserva el original

        for seg, txt in zip(batch, results):
            out.append(
                TranslatedSegment(
                    start=seg.start, end=seg.end,
                    text_original=seg.text, text=(txt or seg.text),
                )
            )

        done = min(i + batch_size, total)
        pct = done / total * 100
        on_progress(
            StageProgress(
                stage="translating", stage_pct=pct,
                message_key="status.translating_progress",
                message_params={"done": done, "total": total},
            )
        )

    on_progress(StageProgress(stage="translating", stage_pct=100.0, message_key="status.translating_done"))
    return out
