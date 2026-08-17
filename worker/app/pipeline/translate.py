"""Traducción de segmentos con deep-translator (GoogleTranslator).

Lógica de negocio TAL CUAL el original: lotes de 25, y si falla el lote
completo se reintenta ítem por ítem, y si un ítem individual falla se
conserva el texto en inglés (degradación silenciosa intencional, no la
tocamos).

Lo nuevo (pedido explícito, la traducción era ~15% del job y son puros
lotes secuenciales de latencia de red): los lotes se mandan en paralelo
con un pool de threads, en vez de uno por uno. Cada thread arma su PROPIA
instancia de `GoogleTranslator` (no comparten una entre sí -- evita
depender de que `requests.Session` sea thread-safe, que no es una
garantía documentada de la librería). Si un lote falla con algo que
parece ser un rate-limit de Google, ESE thread en particular espera con
backoff exponencial y reintenta -- no se baja la concurrencia del pool
entero (no es trivial re-dimensionar un ThreadPoolExecutor en caliente),
pero el efecto práctico es parecido: los threads afectados se frenan
solos sin bloquear a los que están yendo bien.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from app.pipeline.errors import TransientPipelineError
from app.pipeline.progress_types import ProgressCallback, StageProgress, noop_progress
from app.pipeline.transcribe import Segment

logger = logging.getLogger(__name__)

_RATE_LIMIT_MARKERS = ("429", "too many requests", "quota", "rate limit", "rate-limit")
_MAX_BATCH_RETRIES = 3
_BACKOFF_CAP_SECONDS = 8.0


@dataclass(frozen=True)
class TranslatedSegment:
    start: float
    end: float
    text_original: str
    text: str


def _looks_like_rate_limit(exc: Exception) -> bool:
    # deep-translator tira TranslationNotFound cuando Google throttlea bajo
    # carga concurrente -- devuelve "no encontré traducción" en vez de un
    # 429 explícito (verificado en vivo: bajo 4 peticiones en paralelo,
    # ~1 de cada 12 falla así; la MISMA frase en secuencial traduce bien).
    # Lo tratamos como reintentable. El riesgo teórico es una frase
    # genuinamente intraducible que reintentaríamos 3 veces al pedo, pero
    # eso solo cuesta unos segundos de backoff antes de caer al fallback
    # ítem-por-ítem que ya existía -- barato comparado con perder la
    # traducción de un lote entero.
    try:
        from deep_translator.exceptions import TranslationNotFound

        if isinstance(exc, TranslationNotFound):
            return True
    except ImportError:
        pass
    msg = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


def _translate_batch_with_fallback(
    target_lang: str, texts: list[str], batch_label: str,
) -> list[str]:
    """Traduce un lote completo; si parece rate-limit, reintenta con
    backoff exponencial (hasta `_MAX_BATCH_RETRIES` veces). Cualquier otro
    tipo de fallo (o agotar los reintentos de rate-limit) cae al fallback
    ítem por ítem del original, sin cambiarlo."""
    from deep_translator import GoogleTranslator

    translator = GoogleTranslator(source="en", target=target_lang)

    for attempt in range(_MAX_BATCH_RETRIES + 1):
        try:
            return list(translator.translate_batch(texts))
        except Exception as exc:  # noqa: BLE001
            if _looks_like_rate_limit(exc) and attempt < _MAX_BATCH_RETRIES:
                backoff = min(2.0 ** attempt, _BACKOFF_CAP_SECONDS)
                logger.warning(
                    "Rate limit de Google Translate en lote %s (intento %d/%d), esperando %.1fs",
                    batch_label, attempt + 1, _MAX_BATCH_RETRIES, backoff,
                )
                time.sleep(backoff)
                continue
            logger.warning(
                "Fallo el lote de traducción %s (%s: %s), reintentando ítem por ítem",
                batch_label, type(exc).__name__, str(exc)[:200],
            )
            results: list[str] = []
            for t in texts:
                results.append(_translate_one_with_retry(translator, t))
            return results

    return texts  # inalcanzable en la práctica, deja el type checker conforme


def _translate_one_with_retry(translator: Any, text: str) -> str:
    """Traduce un ítem suelto, reintentando con backoff si Google throttlea
    (mismo criterio que el lote). Si agota los reintentos o falla por otra
    causa, conserva el original -- degradación intencional, mejor el texto
    en inglés que perderlo, pero solo DESPUÉS de intentar de verdad."""
    for attempt in range(_MAX_BATCH_RETRIES + 1):
        try:
            return str(translator.translate(text))
        except Exception as exc:  # noqa: BLE001
            if _looks_like_rate_limit(exc) and attempt < _MAX_BATCH_RETRIES:
                time.sleep(min(2.0 ** attempt, _BACKOFF_CAP_SECONDS))
                continue
            return text  # se conserva el original tras agotar reintentos
    return text  # inalcanzable, el for siempre retorna arriba -- para el type checker


def translate(
    segments: list[Segment],
    *,
    target_lang: str = "es",
    batch_size: int = 25,
    # 2 y no 4: con 4 lotes concurrentes, Google throttlea y devuelve
    # TranslationNotFound a ~1 de cada 12 peticiones (verificado en vivo).
    # Con 2 el throttling casi no aparece, y el backoff por ítem cubre los
    # casos residuales. Sigue siendo mucho más rápido que secuencial
    # (185s -> ~30-40s medido) sin romper la traducción.
    max_concurrency: int = 2,
    on_progress: ProgressCallback = noop_progress,
) -> list[TranslatedSegment]:
    on_progress(StageProgress(stage="translating", stage_pct=0.0, message_key="status.translating"))

    try:
        from deep_translator import GoogleTranslator

        GoogleTranslator(source="en", target=target_lang)  # smoke test, ver docstring de más abajo
    except Exception as exc:  # noqa: BLE001 - no controlamos las excepciones de la lib externa
        # Instanciar el traductor solo falla por problemas de red/DNS/servicio
        # caído: es el único punto de translate() que consideramos transitorio
        # y reintentable a nivel tarea Celery. El resto (fallos por lote/ítem)
        # se degrada en silencio más abajo, igual que en el original.
        raise TransientPipelineError(f"No se pudo inicializar el traductor: {exc}") from exc

    total = len(segments)
    if total == 0:
        return []

    batches = [segments[i : i + batch_size] for i in range(0, total, batch_size)]
    results_by_batch: dict[int, list[str]] = {}
    lock = threading.Lock()
    completed_batches = 0

    def _run_batch(idx: int, batch: list[Segment]) -> tuple[int, list[str]]:
        texts = [s.text for s in batch]
        label = f"[{idx * batch_size}:{idx * batch_size + len(batch)}]"
        return idx, _translate_batch_with_fallback(target_lang, texts, label)

    with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as executor:
        futures = [executor.submit(_run_batch, i, b) for i, b in enumerate(batches)]
        for future in as_completed(futures):
            idx, results = future.result()
            with lock:
                results_by_batch[idx] = results
                completed_batches += 1
                done_segments = sum(len(batches[j]) for j in results_by_batch)
                pct = completed_batches / len(batches) * 100
            on_progress(
                StageProgress(
                    stage="translating", stage_pct=pct,
                    message_key="status.translating_progress",
                    message_params={"done": done_segments, "total": total},
                )
            )

    out: list[TranslatedSegment] = []
    for i, batch in enumerate(batches):
        for seg, txt in zip(batch, results_by_batch[i]):
            out.append(
                TranslatedSegment(
                    start=seg.start, end=seg.end,
                    text_original=seg.text, text=(txt or seg.text),
                )
            )

    on_progress(StageProgress(stage="translating", stage_pct=100.0, message_key="status.translating_done"))
    return out
