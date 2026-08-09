"""Configuración de la API, cargada desde variables de entorno."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SUBGEN_", extra="ignore")

    # Redis / Celery (producer-side: la API solo encola y consulta estado,
    # nunca importa el código pesado del worker)
    redis_url: str = Field(default="redis://localhost:6379/0")
    celery_task_always_eager: bool = Field(
        default=False, description="Solo para tests de integración"
    )

    # CORS
    # Guardado como string crudo a propósito: pydantic-settings intenta
    # decodificar campos list[str] como JSON ANTES de correr un
    # field_validator, así que "http://a.com,http://b.com" rompía acá
    # (no es JSON válido). Se expone ya parseado vía la property de abajo.
    cors_origins_raw: str = Field(default="http://localhost:8080", alias="SUBGEN_CORS_ORIGINS")

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]

    # Rate limiting (sintaxis de slowapi/limits: "5/minute", "100/hour", etc.)
    rate_limit_create_job: str = Field(default="5/minute")

    # Validación de negocio
    max_url_length: int = Field(default=2048)
    max_video_duration_seconds: int = Field(default=3600)

    # Almacenamiento S3-compatible (mismo bucket que el worker; ver README)
    s3_endpoint_url: str = Field(default="http://minio:9000")
    s3_public_endpoint_url: str = Field(
        default="", description="Si está vacío, se usa s3_endpoint_url. Necesario cuando el "
        "endpoint interno (red privada/docker) no es accesible desde el navegador del usuario."
    )
    s3_access_key: str = Field(default="subgen")
    s3_secret_key: str = Field(default="subgen12345")
    s3_bucket: str = Field(default="subgen-outputs")
    s3_region: str = Field(default="us-east-1")
    s3_use_ssl: bool = Field(default=False)
    s3_presigned_url_expiry_seconds: int = Field(default=3600)

    # Frontend estático (servido por la propia API, igual que el Flask original)
    serve_frontend: bool = Field(default=True)
    frontend_dir: str = Field(default="/frontend")

    log_level: str = Field(default="INFO")

    @property
    def s3_client_public_endpoint(self) -> str:
        return self.s3_public_endpoint_url or self.s3_endpoint_url


@lru_cache
def get_settings() -> ApiSettings:
    return ApiSettings()
