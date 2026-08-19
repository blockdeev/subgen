# syntax=docker/dockerfile:1
#
# IMPORTANTE: el build context de este Dockerfile es la RAÍZ del repo
# (ver docker-compose.yml: `context: .`, `dockerfile: worker/Dockerfile`),
# igual que api/Dockerfile — así los dos se construyen de forma consistente
# aunque este en particular no necesite copiar nada fuera de `worker/`.

# ── Etapa 1: build (compila deps en un venv aislado) ──────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY worker/pyproject.toml ./
COPY worker/app ./app
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# ── Etapa 2: runtime (imagen final, sin gcc ni cache de pip) ──────────────
FROM python:3.11-slim AS runtime

# El worker SÍ necesita ffmpeg (extracción de audio + quemado de subs).
# procps (ps, top) no es necesario para correr la app — es solo para poder
# diagnosticar el contenedor con `docker exec ... ps aux` cuando hace
# falta (python:3.11-slim no lo trae por default, y nos hizo falta más de
# una vez para debuggear problemas reales de procesos huérfanos).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    procps \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp necesita un runtime de JS para resolver el PoToken de YouTube
# (su sistema de verificación de origen) y acceder al set completo de
# formatos -- sin esto, cae a un subconjunto reducido, que en producción
# se manifestó como "Requested format is not available" (confirmado en
# un deploy real desde IP de datacenter, donde YouTube es más estricto
# que desde una IP residencial). deno es el runtime que yt-dlp detecta
# automático si está en el PATH, sin flags extra.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && apt-get purge -y curl unzip && apt-get autoremove -y

RUN groupadd --gid 1000 subgen && \
    useradd --uid 1000 --gid subgen --shell /bin/bash --create-home subgen

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/home/subgen/.cache/huggingface

WORKDIR /worker
COPY worker/app ./app

# HF_HOME queda como volumen nombrado en el compose: si no, cada rebuild
# de esta imagen tira el modelo Whisper descargado y lo vuelve a bajar de
# HuggingFace desde cero (cientos de MB), no por diseño, era un olvido.
RUN mkdir -p /tmp/subgen "$HF_HOME" && chown -R subgen:subgen /tmp/subgen /worker /home/subgen
USER subgen

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "from app.celery_app import celery_app; celery_app.control.ping(timeout=5) or exit(1)"

# El comando real (worker liso vs. worker+beat en worker-1) se define en el
# docker-compose correspondiente vía `command:`.
CMD ["celery", "-A", "app.celery_app", "worker", "--loglevel=info", "--concurrency=1"]
