"""Acceso de solo lectura al bucket S3-compatible desde la API.

La API nunca sirve archivos leyendo el filesystem por nombre (eso fue lo
que causaba el path-traversal del código original). En vez de eso, resuelve
el `job_id` contra el resultado de Celery, y si el trabajo está completo,
redirige a una URL pre-firmada del objeto correspondiente en el bucket.
El nombre de archivo que ve el usuario sale del resultado guardado por el
worker, nunca de lo que el cliente mande en la URL.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


def _client(settings: Any, *, public: bool = False) -> "S3Client":
    endpoint = settings.s3_client_public_endpoint if public else settings.s3_endpoint_url
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
        config=BotoConfig(signature_version="s3v4"),
    )


def object_exists(settings: Any, key: str) -> bool:
    try:
        _client(settings).head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except ClientError:
        return False


def presigned_download_url(settings: Any, key: str, filename: str) -> str:
    # Firmado contra el endpoint PÚBLICO: la firma incluye el host, así que
    # tiene que ser el mismo host que va a usar el navegador del usuario.
    client = _client(settings, public=True)
    url: str = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=settings.s3_presigned_url_expiry_seconds,
    )
    return url
