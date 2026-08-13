"""Lógica pura para partir un job en N segmentos (quemado paralelo).

Todo acá es sin ffmpeg, sin S3, sin Celery — trabaja sobre listas de
`TranslatedSegment` en memoria, así que es testeable sin infraestructura.

Diseño (ver discusión): el pre-corte del video fuente en N piezas se hace
con `-f segment -c copy` (stream-copy, rapidísimo, sin re-encode) — pero
como es stream-copy, cada corte cae en el keyframe más cercano, NO en el
punto exacto pedido. Por eso las duraciones reales de cada pieza hay que
medirlas con ffprobe DESPUÉS de cortar, nunca asumir los offsets
planeados. Esas duraciones reales son las que alimentan
`boundaries_from_durations` y de ahí `split_cues_by_segment`.
"""
from __future__ import annotations

from app.pipeline.translate import TranslatedSegment


def boundaries_from_durations(durations: list[float]) -> list[float]:
    """Convierte una lista de N duraciones (medidas con ffprobe, en orden)
    en N+1 offsets acumulados: [0, fin_seg_0, fin_seg_1, ..., duración_total].
    """
    if not durations:
        raise ValueError("durations no puede estar vacío")
    boundaries = [0.0]
    for d in durations:
        if d <= 0:
            raise ValueError(f"duración de segmento inválida: {d}")
        boundaries.append(boundaries[-1] + d)
    return boundaries


def split_cues_by_segment(
    cues: list[TranslatedSegment],
    boundaries: list[float],
) -> list[list[TranslatedSegment]]:
    """Divide `cues` (timestamps GLOBALES, sobre el video completo) en
    len(boundaries)-1 listas, una por segmento, según `boundaries`.

    Un cue que cruza una frontera aparece TRUNCADO en cada segmento que
    toca — nunca se descarta ni se duplica sin recortar. Los timestamps de
    salida quedan en tiempo LOCAL de cada segmento (restados contra el
    offset de inicio de ese segmento), listos para pasarle directo a
    `make_srt` sin más ajustes.
    """
    if len(boundaries) < 2:
        raise ValueError("boundaries necesita al menos 2 elementos (inicio y fin)")

    n_segments = len(boundaries) - 1
    result: list[list[TranslatedSegment]] = [[] for _ in range(n_segments)]

    for cue in cues:
        for i in range(n_segments):
            seg_start, seg_end = boundaries[i], boundaries[i + 1]
            overlap_start = max(cue.start, seg_start)
            overlap_end = min(cue.end, seg_end)
            if overlap_end <= overlap_start:
                continue  # este cue no toca este segmento
            result[i].append(
                TranslatedSegment(
                    start=overlap_start - seg_start,
                    end=overlap_end - seg_start,
                    text_original=cue.text_original,
                    text=cue.text,
                )
            )

    return result
