"""Cliente de almacenamiento S3-compatible.

Decisión de arquitectura (ver README): los workers escriben localmente en
`work_dir` durante el pipeline y, al terminar, suben el resultado final
(.srt y opcionalmente el .mp4) a un bucket S3-compatible. La API nunca
toca el filesystem del worker: sirve los archivos generando URLs
pre-firmadas contra el mismo bucket, o haciendo proxy si `s3_presigned_url_expiry_seconds`
se configura en 0 (ver `api/app/routes/downloads.py`).

Funciona igual contra MinIO (self-hosted, incluido en docker-compose.yml
para desarrollo local) o contra Hetzner Object Storage / AWS S3 en
producción: solo cambia `s3_endpoint_url` y las credenciales.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = logging.getLogger(__name__)


class StorageError(Exception):
    pass


def get_client(settings: Any) -> "S3Client":
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
        config=BotoConfig(signature_version="s3v4"),
    )


def ensure_bucket(settings: Any) -> None:
    client = get_client(settings)
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        logger.info("Creando bucket '%s'", settings.s3_bucket)
        client.create_bucket(Bucket=settings.s3_bucket)


def upload_file(settings: Any, local_path: Path, key: str) -> str:
    """Sube un archivo y devuelve la key con la que quedó guardado."""
    client = get_client(settings)
    try:
        client.upload_file(str(local_path), settings.s3_bucket, key)
    except ClientError as exc:
        raise StorageError(f"No se pudo subir {local_path.name} a S3: {exc}") from exc
    return key


def object_exists(settings: Any, key: str) -> bool:
    client = get_client(settings)
    try:
        client.head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except ClientError:
        return False


def delete_object(settings: Any, key: str) -> None:
    client = get_client(settings)
    try:
        client.delete_object(Bucket=settings.s3_bucket, Key=key)
    except ClientError as exc:
        logger.warning("No se pudo borrar %s: %s", key, exc)


def list_objects_older_than(settings: Any, prefix: str, max_age_hours: int) -> list[str]:
    """Usado por la tarea periódica de limpieza (Celery Beat)."""
    import datetime as dt

    client = get_client(settings)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)
    stale_keys: list[str] = []

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                stale_keys.append(obj["Key"])
    return stale_keys
