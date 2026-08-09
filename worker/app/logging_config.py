"""Logging estructurado en JSON, con `job_id` correlacionable entre API y worker.

Se duplica (igual, más chico) en `api/app/logging_config.py` a propósito:
son dos servicios desplegables por separado, no comparten un paquete común,
y esto es lo suficientemente chico como para no justificar un paquete
compartido extra.
"""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Optional

job_id_var: ContextVar[Optional[str]] = ContextVar("job_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        job_id = job_id_var.get()
        if job_id:
            payload["job_id"] = job_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
