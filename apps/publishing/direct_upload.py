"""Direct-to-S3 uploads (Phase 0c, ``cs_direct_upload``).

A large video no longer passes through the web server. The browser asks for a presigned
POST (``start``), sends the file straight to the bucket, then asks us to ``confirm`` it.

- The key is built here, under this account's ``pending/`` prefix; the browser never
  chooses it. The policy pins the key, the type and the exact size, and expires after
  ten minutes.
- What was promised travels in a signed token, so ``confirm`` checks the object against
  it (owner, size, type) without a table of half-finished uploads.
- A confirmed file is copied out of ``pending/``. The bucket's lifecycle rule deletes
  whatever is left there after a day, so an upload that was never confirmed can't be
  published: readiness blocks a ``pending/`` URL too.
- When the browser could hash the file, S3 checks the bytes against that SHA-256 and we
  keep it. Otherwise the ``media`` worker hashes it (``media_checks.run``).

Needs S3 storage and the Phase 0a media checks, so it is only offered when both exist.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from pathlib import PurePosixPath
from urllib.parse import urlparse

from django.conf import settings
from django.core import signing
from django.utils.text import get_valid_filename

from apps.core.releases import is_released

FLAG = "cs_direct_upload"
EXPIRES = 600  # seconds the presigned POST is valid
TOKEN_MAX_AGE = 3600  # seconds from start to confirm, so a slow 300 MB upload fits
IMAGE_MAX_BYTES = 100 * 1024 * 1024  # as the upload through the server
VIDEO_MAX_BYTES = 300 * 1024 * 1024  # Instagram's Reel limit, the largest we publish
CACHE_CONTROL = "max-age=31536000, public"  # as AWS_S3_OBJECT_PARAMETERS
_SALT = "publishing.direct_upload"

ALLOWED_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "video/mp4",
        "video/quicktime",
        "video/x-msvideo",
    }
)


class UploadRefused(Exception):
    """A user-facing reason, with the HTTP status to send it with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def released() -> bool:
    return is_released(FLAG)


def available() -> bool:
    from .constraints import fb_formats_released

    return released() and fb_formats_released() and bool(settings.USE_S3_STORAGE)


def _root() -> str:
    return getattr(settings, "MEDIAFILES_LOCATION", "media")


def pending_prefix(account) -> str:
    return f"{_root()}/pending/publishing/{account.pk}/"


def _final_key(pending_key: str) -> str:
    return f"{_root()}/" + pending_key[len(f"{_root()}/pending/") :]


def is_unconfirmed(url: str) -> bool:
    """A URL still under ``pending/``: uploaded but never confirmed."""
    from .services import _is_own_media_url

    return _is_own_media_url(url) and f"/{_root()}/pending/" in urlparse(url).path


def _client():
    import boto3
    from botocore.config import Config

    region = settings.AWS_S3_REGION_NAME or None
    # The browser posts to the regional endpoint: the global one can answer with a
    # redirect, which a cross-origin POST can't follow.
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com" if region else None,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
    )


def _safe_name(name: str) -> str:
    path = PurePosixPath((name or "").replace("\\", "/"))
    base, ext = path.stem if path.suffix else path.name, path.suffix
    try:
        base = get_valid_filename(base)[:80]
    except Exception:  # nothing usable left
        base = ""
    return f"{base or 'file'}{get_valid_filename(ext)[:10] if ext else ''}"


def _sha256_b64(value: str) -> str:
    """``value`` if it is a base64 SHA-256 digest, else "" (the server hashes instead)."""
    try:
        return value if len(base64.b64decode(value, validate=True)) == 32 else ""
    except (binascii.Error, ValueError, TypeError):
        return ""


def _limit(content_type: str) -> int:
    return VIDEO_MAX_BYTES if content_type.startswith("video/") else IMAGE_MAX_BYTES


def start(account, *, name: str, content_type: str, size, sha256: str = "") -> dict:
    """A presigned POST for one file: ``{url, fields, token}``."""
    if content_type not in ALLOWED_TYPES:
        raise UploadRefused(f'"{content_type}" is not a supported media type.')
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        raise UploadRefused("The file is empty.")
    limit = _limit(content_type)
    if size > limit:
        raise UploadRefused(f"File exceeds the {limit // (1024 * 1024)} MB limit.")
    sha256 = _sha256_b64(sha256 or "")

    key = f"{pending_prefix(account)}{uuid.uuid4().hex}/{_safe_name(name)}"
    fields = {"Content-Type": content_type}
    conditions: list = [
        {"Content-Type": content_type},
        ["content-length-range", size, size],
    ]
    if sha256:
        fields["x-amz-checksum-algorithm"] = "SHA256"
        fields["x-amz-checksum-sha256"] = sha256
        conditions += [
            {"x-amz-checksum-algorithm": "SHA256"},
            {"x-amz-checksum-sha256": sha256},
        ]
    post = _client().generate_presigned_post(
        settings.AWS_STORAGE_BUCKET_NAME,
        key,
        Fields=fields,
        Conditions=conditions,
        ExpiresIn=EXPIRES,
    )
    token = signing.dumps(
        {"a": account.pk, "k": key, "t": content_type, "s": size, "h": sha256},
        salt=_SALT,
    )
    return {"url": post["url"], "fields": post["fields"], "token": token}


def _head(client, key: str) -> dict | None:
    from botocore.exceptions import ClientError

    try:
        return client.head_object(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key, ChecksumMode="ENABLED"
        )
    except ClientError as exc:
        code = str((exc.response.get("Error") or {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def _hex(sha_b64: str) -> str:
    # A multipart object's checksum ("…-N") is of its parts, not of the file.
    if not sha_b64 or "-" in sha_b64:
        return ""
    try:
        return base64.b64decode(sha_b64).hex()
    except (binascii.Error, ValueError):
        return ""


def confirm(account, token: str, user=None) -> dict:
    """Check the uploaded object against what ``start`` promised, move it out of
    ``pending/`` and start its media check. Returns ``{url, name, content_type}``.
    Safe to repeat: a second confirm of the same upload returns the same file."""
    from django.core.files.storage import default_storage

    from . import media_checks

    try:
        data = signing.loads(token or "", salt=_SALT, max_age=TOKEN_MAX_AGE)
    except signing.SignatureExpired:
        raise UploadRefused(
            "This upload took too long. Upload the file again.", 410
        ) from None
    except signing.BadSignature:
        raise UploadRefused("This upload isn't valid.", 403) from None
    key = data.get("k", "")
    if data.get("a") != account.pk or not key.startswith(pending_prefix(account)):
        raise UploadRefused("This upload isn't valid.", 403)

    bucket = settings.AWS_STORAGE_BUCKET_NAME
    client = _client()
    final = _final_key(key)
    head = _head(client, key)
    if head is None:
        head = _head(client, final)  # confirmed already (a retried request)
        if head is None:
            raise UploadRefused("The upload didn't finish. Upload the file again.", 409)
    else:
        if (
            head.get("ContentLength") != data["s"]
            or head.get("ContentType") != data["t"]
        ):
            client.delete_object(Bucket=bucket, Key=key)
            raise UploadRefused(
                "The uploaded file doesn't match what was started. Upload it again."
            )
        if data.get("h") and head.get("ChecksumSHA256") != data["h"]:
            client.delete_object(Bucket=bucket, Key=key)
            raise UploadRefused("The upload was damaged. Upload the file again.")
        client.copy_object(
            Bucket=bucket,
            Key=final,
            CopySource={"Bucket": bucket, "Key": key},
            MetadataDirective="REPLACE",
            ContentType=data["t"],
            CacheControl=CACHE_CONTROL,
            ChecksumAlgorithm="SHA256",
        )
        client.delete_object(Bucket=bucket, Key=key)

    url = default_storage.url(final[len(f"{_root()}/") :])
    media_checks.record_direct_upload(
        account,
        url,
        kind="video" if data["t"].startswith("video/") else "image",
        size_bytes=data["s"],
        sha256=_hex(data.get("h") or head.get("ChecksumSHA256", "")),
    )
    from .media_library import record_if_released

    record_if_released(
        account,
        url,
        kind="video" if data["t"].startswith("video/") else "image",
        mime=data["t"],
        name=key.rsplit("/", 1)[-1],
        size_bytes=data["s"],
        sha256=_hex(data.get("h") or head.get("ChecksumSHA256", "")),
        user=user,
    )
    return {"url": url, "name": key.rsplit("/", 1)[-1], "content_type": data["t"]}
