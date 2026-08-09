"""Tipos compartidos para reportar progreso desde las etapas del pipeline.

Cada etapa del pipeline (download, transcribe, translate, burn) recibe un
callback `on_progress` opcional y lo llama con un `StageProgress`. El
callback no sabe nada de Celery ni de Redis: eso se resuelve en
`worker/app/tasks.py`, así las funciones acá son puras y fáciles de testear.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class StageProgress:
    stage: str  # "downloading" | "transcribing" | "translating" | "burning"
    stage_pct: float  # 0.0–100.0, progreso DENTRO de la etapa
    message_key: str  # clave de traducción, ej. "status.downloading"
    message_params: dict[str, Any] = field(default_factory=dict)
    eta_seconds: Optional[float] = None


ProgressCallback = Callable[[StageProgress], None]


def noop_progress(_: StageProgress) -> None:
    """Callback por defecto: no hace nada. Útil en tests y uso standalone."""
    return None
