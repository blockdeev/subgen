"""Generación de .srt y utilidades puras (timestamps, sanitización de nombres).

`fmt_ts` y la construcción del SRT son las mismas del original. Se agrega
`sanitize_filename` con protección explícita contra path traversal (el
filtro alnum/espacio/guion del original ya era seguro en la práctica, pero
acá lo hacemos explícito y a prueba de nombres vacíos/reservados).
"""
from __future__ import annotations

import re
from pathlib import Path

from app.pipeline.translate import TranslatedSegment

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9 _-]")
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *[f"com{i}" for i in range(1, 10)],
                    *[f"lpt{i}" for i in range(1, 10)]}


def fmt_ts(sec: float) -> str:
    """Formatea segundos a timestamp SRT: HH:MM:SS,mmm.

    Calculado en milisegundos totales (no h/m/s por separado + redondeo de
    ms al final) para que un redondeo como 1.9999s -> 2000ms acarree
    correctamente el segundo en vez de producir "01,1000".
    """
    total_ms = int(round(sec * 1000))
    h, rem_ms = divmod(total_ms, 3_600_000)
    m, rem_ms = divmod(rem_ms, 60_000)
    s, ms = divmod(rem_ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt_content(segments: list[TranslatedSegment]) -> str:
    lines: list[str] = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i), f"{fmt_ts(seg.start)} --> {fmt_ts(seg.end)}", seg.text, ""]
    return "\n".join(lines)


def make_srt(segments: list[TranslatedSegment], path: Path) -> Path:
    path.write_text(build_srt_content(segments), encoding="utf-8")
    return path


def sanitize_filename(title: str, *, max_length: int = 80, fallback: str = "video") -> str:
    """Deriva un nombre de archivo seguro a partir del título del video.

    - Solo permite [a-zA-Z0-9 _-] (mismo criterio que el original).
    - Colapsa espacios repetidos y recorta bordes.
    - Nunca produce '..', nombres vacíos, ni nombres reservados de Windows
      (por si el storage compartido termina montado desde un host Windows).
    - Trunca a max_length preservando palabras completas cuando es posible.
    """
    safe = _UNSAFE_CHARS.sub("", title)
    safe = re.sub(r"\s+", " ", safe).strip(" .-_")
    safe = safe[:max_length].strip(" .-_")

    if not safe or safe.replace(".", "").replace("-", "").replace("_", "") == "":
        safe = fallback
    if safe.lower() in _RESERVED_NAMES:
        safe = f"{safe}_file"
    return safe
