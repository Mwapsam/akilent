"""Backup status for the Pilot Command Center, read from the private backup bucket.

The ``backup`` service (docker/backup/backup.sh) writes ``db/YYYY-MM-DD.dump`` nightly and
``db/restore-check-latest.json`` weekly. This only lists and reads them; it never writes.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_KEY = "ops:backup-status"
CACHE_SECONDS = 600


def status() -> dict:
    """``{"configured", "latest", "latest_at", "size_mb", "restore", "error"}``."""
    if not settings.BACKUP_S3_BUCKET:
        return {"configured": False}
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    result = _read(settings.BACKUP_S3_BUCKET)
    cache.set(CACHE_KEY, result, CACHE_SECONDS)
    return result


def _read(bucket: str) -> dict:
    result = {"configured": True, "latest": None, "latest_at": None, "size_mb": None,
              "restore": None, "error": ""}
    try:
        import boto3

        s3 = boto3.client("s3")
        dumps = [o for o in s3.list_objects_v2(Bucket=bucket, Prefix="db/").get("Contents", [])
                 if o["Key"].endswith(".dump")]
        if dumps:
            newest = max(dumps, key=lambda o: o["LastModified"])
            result.update(latest=newest["Key"].removeprefix("db/"), latest_at=newest["LastModified"],
                          size_mb=round(newest["Size"] / 1_000_000, 1))
        try:
            body = s3.get_object(Bucket=bucket, Key="db/restore-check-latest.json")["Body"].read()
            result["restore"] = json.loads(body)
        except s3.exceptions.NoSuchKey:
            pass
    except Exception as exc:  # the page must render even if S3 is unreachable
        logger.warning("backup status unavailable: %s", exc)
        result["error"] = "Couldn't read the backup bucket. Check the web app's AWS access to it."
    return result
