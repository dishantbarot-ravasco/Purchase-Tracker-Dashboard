"""
apps/services/object_storage.py - Cloudflare R2 object storage (2026-09-30).

R2 speaks the S3 API, so this is a thin boto3 wrapper pointed at the
account's R2 endpoint. Every bucket is private: nothing here makes an object
public, and a file is handed to a browser only through a short-lived
presigned link.

A bucket is named by its kind - "po", "invoice" or "backup" - never by its
real name, which lives in settings.R2_BUCKETS (env vars R2_BUCKET_PO,
R2_BUCKET_INVOICE, R2_BUCKET_BACKUPS). Blank settings switch storage off;
every call then raises StorageNotConfigured naming what is missing, so a
misconfigured deploy fails loudly instead of quietly skipping a backup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings

KINDS = ("po", "invoice", "backup")


class StorageNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    last_modified: datetime


def _bucket(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"Unknown storage kind {kind!r}; expected one of {', '.join(KINDS)}.")
    missing = [name for name, value in (
        ("R2_ACCOUNT_ID", settings.R2_ACCOUNT_ID),
        ("R2_ACCESS_KEY_ID", settings.R2_ACCESS_KEY_ID),
        ("R2_SECRET_ACCESS_KEY", settings.R2_SECRET_ACCESS_KEY),
        (f"R2_BUCKET_{'BACKUPS' if kind == 'backup' else kind.upper()}", settings.R2_BUCKETS.get(kind)),
    ) if not value]
    if missing:
        raise StorageNotConfigured(f"Object storage is not configured: set {', '.join(missing)}.")
    return settings.R2_BUCKETS[kind]


def require_configured(kind: str) -> None:
    """Raise StorageNotConfigured, naming the missing variables, unless the
    kind's bucket is fully set up."""
    _bucket(kind)


def is_configured(kind: str) -> bool:
    try:
        require_configured(kind)
    except StorageNotConfigured:
        return False
    return True


def _client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT_URL or f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
    )


def upload_file(kind: str, key: str, path: str, content_type: str = "application/octet-stream") -> None:
    """Upload a local file. boto3 switches to a multipart upload for large
    files on its own, so a growing database dump needs no special case."""
    bucket = _bucket(kind)
    _client().upload_file(path, bucket, key, ExtraArgs={"ContentType": content_type})


def download_file(kind: str, key: str, path: str) -> None:
    """Download one object to a local file (verify_backup's restore check)."""
    _client().download_file(_bucket(kind), key, path)


def list_objects(kind: str, prefix: str = "") -> list[StoredObject]:
    bucket = _bucket(kind)
    paginator = _client().get_paginator("list_objects_v2")
    found = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            found.append(StoredObject(key=item["Key"], size=item["Size"], last_modified=item["LastModified"]))
    return found


def delete_objects(kind: str, keys: list[str]) -> None:
    bucket = _bucket(kind)
    client = _client()
    # The S3 API takes at most 1000 keys per delete call.
    for start in range(0, len(keys), 1000):
        chunk = keys[start:start + 1000]
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})


def presigned_url(kind: str, key: str, expires_seconds: int = 300) -> str:
    """A link that opens one private object for a few minutes."""
    bucket = _bucket(kind)
    return _client().generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_seconds,
    )
