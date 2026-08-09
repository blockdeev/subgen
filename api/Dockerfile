# syntax=docker/dockerfile:1
#
# IMPORTANTE: el build context de este Dockerfile es la RAÍZ del repo
# (ver docker-compose.yml: `context: .`, `dockerfile: api/Dockerfile`),
# no `api/`. Así puede copiar `frontend/` además de `api/app`.

# ── Etapa 1: build ──────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY api/pyproject.toml ./
COPY api/app ./app
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# ── Etapa 2: runtime ────────────────────────────────────────────────────
# La API NO necesita FFmpeg: eso es del worker únicamente.
FROM python:3.11-slim AS runtime

RUN groupadd --gid 1000 subgen && \
    useradd --uid 1000 --gid subgen --shell /bin/bash --create-home subgen

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /api
COPY api/app ./app
COPY --chown=subgen:subgen frontend /frontend

USER subgen

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=3)" || exit 1

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
