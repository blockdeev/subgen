"""Esquemas Pydantic de request/response. Toda la validación de entrada
(URL, idioma, modo) vive acá, no en las rutas."""
from __future__ import annotations

from typing import Any, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

JobMode = Literal["srt", "video"]
JobStatus = Literal[
    "queued", "downloading", "transcribing", "translating",
    "burning", "completed", "error", "retrying",
]

_ALLOWED_SCHEMES = {"http", "https"}
# Idiomas soportados por GoogleTranslator (deep-translator) que ya estaban
# en el <select> del frontend original.
_ALLOWED_TARGET_LANGS = {"es", "pt", "fr", "de", "it", "ja", "ko", "zh-CN"}


class JobCreateRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    target_lang: str = Field(default="es")
    mode: JobMode = Field(default="srt")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        parsed = urlparse(v)
        if parsed.scheme not in _ALLOWED_SCHEMES:
            raise ValueError("La URL debe empezar con http:// o https://")
        if not parsed.netloc:
            raise ValueError("URL inválida: falta el dominio")
        return v

    @field_validator("target_lang")
    @classmethod
    def validate_target_lang(cls, v: str) -> str:
        if v not in _ALLOWED_TARGET_LANGS:
            raise ValueError(f"Idioma de traducción no soportado: {v}")
        return v


class JobCreateResponse(BaseModel):
    job_id: str


class PreviewSegment(BaseModel):
    start: float
    end: float
    text: str
    text_original: str


class JobResult(BaseModel):
    title: str
    duration: float
    segments_count: int
    srt_key: str
    srt_filename: str
    mode: JobMode
    preview_segments: list[PreviewSegment] = Field(default_factory=list)
    video_key: Optional[str] = None
    video_filename: Optional[str] = None
    video_size_mb: Optional[float] = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: float = 0.0
    stage_progress: Optional[float] = None
    message_key: Optional[str] = None
    message_params: dict[str, Any] = Field(default_factory=dict)
    eta_seconds: Optional[int] = None
    result: Optional[JobResult] = None


class ErrorResponse(BaseModel):
    error: str
