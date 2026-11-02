"""Server-side media checks (Phase 0a): a file's SHA-256, and for a video its duration,
size and frame rate, read by ffprobe on the ``media`` queue.

The composer's own reading of a video is only a hint; readiness enforces these. A check
still running blocks scheduling and publishing ("Checking…"), and one that failed blocks
with its reason. Checks run on their own worker (``celery_media``), so a batch of large
uploads never delays publishing.

Only our own media host is fetched or probed: probing an arbitrary URL would let a user
make the server request internal addresses (SSRF).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from datetime import timedelta
from fractions import Fraction

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import constraints as limits
from .models import MediaCheck

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 120  # seconds; MP4s with their index at the end read slowly over HTTP
SLOW_AFTER = timedelta(minutes=3)  # the composer says checks are running slowly
BACKLOG_ALERT_AFTER = timedelta(minutes=10)  # the oldest pending check alerts
RECHECK_AFTER = timedelta(minutes=15)  # a failed check is tried again after this
_HASH_CHUNK = 1024 * 1024
UNCONFIRMED_MESSAGE = "This upload didn't finish. Upload the file again."


def url_digest(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def find(account, url: str) -> MediaCheck | None:
    return MediaCheck.objects.filter(
        account=account, url_digest=url_digest(url)
    ).first()


def _get_or_create(account, url: str, **defaults) -> tuple[MediaCheck, bool]:
    try:
        with transaction.atomic():
            return MediaCheck.objects.get_or_create(
                account=account,
                url_digest=url_digest(url),
                defaults={"url": url, **defaults},
            )
    except IntegrityError:
        # Another worker created it first, so it exists now.
        return MediaCheck.objects.get(
            account=account, url_digest=url_digest(url)
        ), False


def _enqueue(check: MediaCheck) -> None:
    from .tasks import probe_media

    check.queued_at = timezone.now()
    MediaCheck.objects.filter(pk=check.pk).update(queued_at=check.queued_at)
    transaction.on_commit(lambda: probe_media.delay(check.pk), robust=True)


def record_upload(account, url: str, data: bytes, content_type: str) -> MediaCheck:
    """An upload through Akilent: the bytes are in hand, so hash them now. An image is
    done; a video still needs ffprobe."""
    kind = "video" if content_type.startswith("video/") else "image"
    return _record(account, url, kind, len(data), hashlib.sha256(data).hexdigest())


def record_direct_upload(
    account, url: str, *, kind: str, size_bytes: int, sha256: str
) -> MediaCheck:
    """A confirmed direct-to-S3 upload (Phase 0c). ``sha256`` is the one S3 verified,
    or "" when the browser couldn't hash the file; the media worker hashes it then."""
    return _record(account, url, kind, size_bytes, sha256)


def _record(account, url: str, kind: str, size_bytes: int, sha256: str) -> MediaCheck:
    done = kind != "video" and bool(sha256)
    values = {
        "kind": kind,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "status": MediaCheck.Status.OK if done else MediaCheck.Status.PENDING,
        "checked_at": timezone.now() if done else None,
    }
    check, created = _get_or_create(account, url, **values)
    if not created:
        for field, value in values.items():
            setattr(check, field, value)
        check.error = ""
        check.save()
    if not done:
        _enqueue(check)
    return check


def ensure(account, url: str) -> MediaCheck:
    """The check for ``url``, started now if the file has none (media added before
    checks existed, or by another route). A failed check is run again once it is
    ``RECHECK_AFTER`` old, since a timeout or a storage blip isn't the file's fault."""
    check, created = _get_or_create(
        account, url, kind=limits.media_kind(url), status=MediaCheck.Status.PENDING
    )
    if created:
        _enqueue(check)
    elif check.status == MediaCheck.Status.FAILED and (
        check.checked_at is not None
        and check.checked_at <= timezone.now() - RECHECK_AFTER
    ):
        # Conditional, so two readiness calls at once queue one probe.
        if MediaCheck.objects.filter(
            pk=check.pk, status=MediaCheck.Status.FAILED, checked_at=check.checked_at
        ).update(status=MediaCheck.Status.PENDING, error=""):
            check.status, check.error = MediaCheck.Status.PENDING, ""
            _enqueue(check)
    return check


# --- Running a check (the ``media`` queue) ----------------------------------------------


def _ffprobe(url: str) -> dict:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise RuntimeError("ffprobe isn't installed on this server.")
    out = subprocess.run(  # noqa: S603 - fixed binary, URL limited to our media host
        [
            binary,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            url,
        ],
        capture_output=True,
        timeout=PROBE_TIMEOUT,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError((out.stderr or b"").decode(errors="replace")[:200])
    return json.loads(out.stdout or b"{}")


def _rate(value: str) -> float | None:
    try:
        rate = Fraction(value or "0")
    except (ValueError, ZeroDivisionError):
        return None
    return float(rate) if rate else None


def _parse(data: dict) -> dict:
    video = next(
        (s for s in data.get("streams") or [] if s.get("codec_type") == "video"), None
    )
    if video is None:
        raise RuntimeError("The file has no video track.")
    fmt = data.get("format") or {}
    duration = video.get("duration") or fmt.get("duration")
    size = fmt.get("size")
    width, height = video.get("width"), video.get("height")
    # A phone video's rotation tag swaps what the viewer sees.
    rotation = abs(int((video.get("tags") or {}).get("rotate", 0) or 0))
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = abs(int(side["rotation"]))
    if rotation in (90, 270) and width and height:
        width, height = height, width
    return {
        "duration": float(duration) if duration else None,
        "width": width,
        "height": height,
        "fps": _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")),
        "size_bytes": int(size) if size else None,
    }


def _stream_hash(url: str) -> str:
    import requests

    digest = hashlib.sha256()
    with requests.get(url, stream=True, timeout=30, allow_redirects=False) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def run(check: MediaCheck) -> MediaCheck:
    """Probe and hash ``check``'s file, and record the outcome."""
    from .direct_upload import is_unconfirmed
    from .services import _is_own_media_url

    fields: dict = {}
    try:
        if not _is_own_media_url(check.url):
            raise RuntimeError(
                "Upload this file to Akilent so it can be checked before publishing."
            )
        if is_unconfirmed(check.url):
            raise RuntimeError(UNCONFIRMED_MESSAGE)
        if (check.kind or limits.media_kind(check.url)) == "video":
            fields.update(_parse(_ffprobe(check.url)))
            fields["kind"] = "video"
        if not check.sha256:
            fields["sha256"] = _stream_hash(check.url)
        fields["status"] = MediaCheck.Status.OK
        fields["error"] = ""
    except subprocess.TimeoutExpired:
        fields = {
            "status": MediaCheck.Status.FAILED,
            "error": "Checking the file took too long. Try a shorter or smaller file.",
        }
    except Exception as exc:
        logger.info("Media check failed for check=%s: %s", check.pk, exc)
        fields = {
            "status": MediaCheck.Status.FAILED,
            "error": str(exc)[:300] or "The file couldn't be read.",
        }
    fields["checked_at"] = timezone.now()
    for name, value in fields.items():
        setattr(check, name, value)
    check.save(update_fields=list(fields))
    return check


# --- What readiness asks ---------------------------------------------------------------


def violations(check: MediaCheck, platform: str, media_type: str) -> list[str]:
    """The ways ``check``'s video breaks ``platform``'s limits for ``media_type``."""
    rules = limits.VIDEO_LIMITS.get((platform, media_type))
    if not rules or check.kind != "video":
        return []
    out = []
    lo, hi = rules.get("duration", (None, None))
    if check.duration is not None:
        if lo is not None and check.duration < lo:
            out.append(f"at least {lo} seconds long")
        if hi is not None and check.duration > hi:
            out.append(f"at most {_seconds(hi)} long")
    lo, hi = rules.get("fps", (None, None))
    if check.fps is not None and (
        (lo is not None and check.fps < lo - 0.5)
        or (hi is not None and check.fps > hi + 0.5)
    ):
        out.append(f"{lo} to {hi} frames per second")
    min_size = rules.get("min_size")
    if min_size and check.width and check.height:
        if check.width < min_size[0] or check.height < min_size[1]:
            out.append(f"at least {min_size[0]} by {min_size[1]} pixels")
    ratio = rules.get("ratio")
    if ratio and check.width and check.height:
        r = check.width / check.height
        if not ratio[0] <= r <= ratio[1]:
            out.append("vertical 9:16" if ratio[1] < 1 else "a supported shape")
    max_bytes = rules.get("max_bytes")
    if max_bytes and check.size_bytes and check.size_bytes > max_bytes:
        out.append(f"at most {max_bytes // (1024 * 1024)} MB")
    return out


def _seconds(value: int) -> str:
    if value >= 60 and value % 60 == 0:
        minutes = value // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{value} seconds"


def is_slow(check: MediaCheck, now=None) -> bool:
    now = now or timezone.now()
    return (
        check.status == MediaCheck.Status.PENDING
        and (check.queued_at or check.created_at) < now - SLOW_AFTER
    )


def backlog(now=None):
    """Checks pending longer than the alert threshold, oldest first."""
    now = now or timezone.now()
    since = now - BACKLOG_ALERT_AFTER
    return (
        MediaCheck.objects.filter(status=MediaCheck.Status.PENDING)
        .filter(
            Q(queued_at__lt=since) | Q(queued_at__isnull=True, created_at__lt=since)
        )
        .order_by("created_at")
    )
