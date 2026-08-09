"""Jerarquía de excepciones compartida por las etapas del pipeline.

`tasks.py` decide la política de reintentos mirando el TIPO de excepción,
nunca el texto del mensaje. Cada etapa (`download`, `translate`) lanza una
subclase concreta; `burn` y `transcribe` lanzan sus propios errores
(`BurnError`, ausencia de segmentos) que se tratan siempre como
determinísticos: no tiene sentido reintentar un video corrupto o sin habla.
"""
from __future__ import annotations


class DeterministicPipelineError(Exception):
    """El resultado no va a cambiar si se reintenta (URL inválida, sin audio,
    formato no soportado, etc.). La tarea Celery NO debe reintentar esto."""


class TransientPipelineError(Exception):
    """Fallo pasajero (red, timeout, servicio temporalmente no disponible).
    La tarea Celery SÍ debe reintentar esto con backoff."""
