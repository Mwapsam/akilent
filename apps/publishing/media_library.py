"""The media library (Phase 3, ``cs_media_library``).

Every file uploaded to Content Studio (the composer, direct uploads, CSV imports) is
recorded as a ``MediaAsset`` once the library is released. Posts keep their media as
URLs, so where an asset is used is read from the posts each time (``usage``).

Removing an asset follows one rule (``remove``):

- a draft, scheduled, publishing or still-being-checked post uses it: **blocked**;
- only posts that are published (or failed or cancelled) use it: **archived**, hidden
  from the library with the file kept, because those posts still point at it;
- nothing uses it: **deleted**, file and all.

Edits never change the original: a crop, a text layer or a Reel cover frame is a new
asset whose ``parent`` is the original.

Stock photos come from Pexels, whose terms allow using its photos for anything within
its guidelines and ask for a link to Pexels and credit to the photographer. The credit
is stored with the asset. Unsplash's API guidelines require hotlinking for all uses,
which a copy that Meta fetches from our storage would not be, so it isn't offered.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import subprocess
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from . import images
from .models import ContentPost, ContentPostVariant, MediaAsset

logger = logging.getLogger(__name__)

FLAG = "cs_media_library"
IMAGE_TYPES = images.IMAGE_TYPES
EDIT_MAX_BYTES = 30 * 1024 * 1024
COVER_TIMEOUT = 60  # seconds for ffmpeg to grab one frame over HTTP
TAG_MAX, TAGS_MAX = 30, 20
PEXELS_API = "https://api.pexels.com/v1"
PEXELS_IMAGE_HOST = "images.pexels.com"

# A post in one of these states still needs its media.
LIVE_POST_STATUSES = (
    ContentPost.Status.IDEA,
    ContentPost.Status.DRAFT,
    ContentPost.Status.IN_REVIEW,
    ContentPost.Status.APPROVED,
    ContentPost.Status.SCHEDULED,
    ContentPost.Status.PUBLISHING,
)
LIVE_VARIANT_STATUSES = (
    ContentPostVariant.Status.SCHEDULED,
    ContentPostVariant.Status.PUBLISHING,
    ContentPostVariant.Status.PROCESSING,
    ContentPostVariant.Status.RECONCILING,
)


class LibraryError(ValueError):
    """The request can't be done; the message is for the person."""


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def url_digest(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def name_from_url(url: str) -> str:
    """A file's name as people wrote it: the URL's last part with %20 and the like
    decoded, and no query string."""
    from urllib.parse import unquote, urlsplit

    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def _dimensions(data: bytes) -> tuple[int | None, int | None]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return None, None


def record(
    account,
    url: str,
    *,
    kind: str,
    mime: str = "",
    name: str = "",
    data: bytes | None = None,
    size_bytes: int | None = None,
    sha256: str = "",
    source: str = MediaAsset.Source.UPLOAD,
    user=None,
    parent: MediaAsset | None = None,
    credit: str = "",
    credit_url: str = "",
) -> MediaAsset:
    """The asset for ``url``, created on first sight."""
    values = {
        "url": url,
        "kind": kind,
        "mime": mime[:50],
        "name": (name or name_from_url(url))[:200],
        "size_bytes": len(data) if data is not None else size_bytes,
        "sha256": hashlib.sha256(data).hexdigest() if data is not None else sha256,
        "source": source,
        "uploaded_by": user if getattr(user, "is_authenticated", False) else None,
        "parent": parent,
        "credit": credit[:200],
        "credit_url": credit_url[:500],
    }
    if data is not None and kind == "image":
        values["width"], values["height"] = _dimensions(data)
    try:
        with transaction.atomic():
            asset, _ = MediaAsset.objects.get_or_create(
                account=account, url_digest=url_digest(url), defaults=values
            )
    except IntegrityError:
        asset = MediaAsset.objects.get(account=account, url_digest=url_digest(url))
    return asset


def record_if_released(account, url: str, **kwargs) -> MediaAsset | None:
    if not is_released():
        return None
    try:
        return record(account, url, **kwargs)
    except Exception:
        # The library is a convenience: an upload must never fail because of it.
        logger.exception("media library record failed for account=%s", account.pk)
        return None


def sync_from_check(asset: MediaAsset) -> None:
    """Copy what the media check found (Phase 0a) onto the asset."""
    from .models import MediaCheck

    check = MediaCheck.objects.filter(
        account_id=asset.account_id,
        url_digest=url_digest(asset.url),
        status=MediaCheck.Status.OK,
    ).first()
    if check is None:
        return
    fields = []
    for name in ("width", "height", "duration", "fps", "sha256", "size_bytes"):
        value = getattr(check, name)
        if value and not getattr(asset, name):
            setattr(asset, name, value)
            fields.append(name)
    if fields:
        asset.save(update_fields=fields)


_CHECKED_FIELDS = ("width", "height", "duration", "fps", "sha256", "size_bytes")


def sync_from_checks(assets) -> None:
    """``sync_from_check`` for a page of assets: one query for the checks, and a save
    only for assets that gain something."""
    from .models import MediaCheck

    missing = [a for a in assets if not all(getattr(a, f) for f in _CHECKED_FIELDS)]
    if not missing:
        return
    by_digest = {url_digest(a.url): a for a in missing}
    checks = MediaCheck.objects.filter(
        account_id=missing[0].account_id,
        url_digest__in=list(by_digest),
        status=MediaCheck.Status.OK,
    )
    for check in checks:
        asset = by_digest[check.url_digest]
        fields = []
        for name in _CHECKED_FIELDS:
            value = getattr(check, name)
            if value and not getattr(asset, name):
                setattr(asset, name, value)
                fields.append(name)
        if fields:
            asset.save(update_fields=fields)


def usage_counts(account, assets) -> dict[int, int]:
    """How many posts use each asset (as ``usage`` counts them), for a page of assets in
    two queries instead of one per asset."""
    urls = {a.url.lower(): a.pk for a in assets}
    if not urls:
        return {}
    match = Q()
    for url in urls:
        match |= Q(media_urls__icontains=url) | Q(variants__options__icontains=url)
    posts = (
        ContentPost.objects.filter(account=account)
        .filter(match)
        .distinct()
        .only("pk", "media_urls")
        .prefetch_related(
            Prefetch(
                "variants",
                queryset=ContentPostVariant.objects.only("post_id", "options"),
            )
        )
    )
    used: dict[int, set[int]] = {}
    for post in posts:
        text = json.dumps(
            [post.media_urls or [], [v.options or {} for v in post.variants.all()]],
            ensure_ascii=False,
        ).lower()
        for url, pk in urls.items():
            if url in text:
                used.setdefault(pk, set()).add(post.pk)
    return {pk: len(post_ids) for pk, post_ids in used.items()}


# --- Where it's used, and removing it ----------------------------------------------------


def usage(asset: MediaAsset):
    """Posts of this business that use the asset, as media or as a Reel cover."""
    return (
        ContentPost.objects.filter(account_id=asset.account_id)
        .filter(
            Q(media_urls__icontains=asset.url)
            | Q(variants__options__icontains=asset.url)
        )
        .distinct()
    )


def blocking_posts(asset: MediaAsset):
    return (
        usage(asset)
        .filter(
            Q(status__in=LIVE_POST_STATUSES)
            | Q(variants__status__in=LIVE_VARIANT_STATUSES)
        )
        .distinct()
    )


def _storage_name(url: str) -> str | None:
    base = settings.MEDIA_URL or ""
    if base and url.startswith(base):
        return url[len(base) :]
    return None


def remove(asset: MediaAsset) -> tuple[str, list]:
    """``("blocked", posts)``, ``("archived", posts)`` or ``("deleted", [])``."""
    blocking = list(blocking_posts(asset)[:5])
    if blocking:
        return "blocked", blocking
    used = list(usage(asset)[:5])
    if used:
        asset.archived_at = timezone.now()
        asset.save(update_fields=["archived_at"])
        return "archived", used
    name = _storage_name(asset.url)
    asset.delete()
    if name:
        try:
            default_storage.delete(name)
        except Exception:
            logger.warning("Couldn't delete media file %s", name, exc_info=True)
    return "deleted", []


def set_tags(asset: MediaAsset, raw: str) -> None:
    tags = []
    for t in (raw or "").replace("#", "").split(","):
        t = " ".join(t.split()).lower()[:TAG_MAX]
        if t and t not in tags:
            tags.append(t)
    asset.tags = tags[:TAGS_MAX]
    asset.save(update_fields=["tags"])


# --- Edited copies -----------------------------------------------------------------------


def _save(account, folder: str, ext: str, data: bytes) -> str:
    path = default_storage.save(
        f"publishing/{account.pk}/{folder}/{uuid.uuid4().hex}.{ext}", ContentFile(data)
    )
    return default_storage.url(path)


def save_edit(asset: MediaAsset, upload, user) -> MediaAsset:
    """A cropped, rotated or captioned copy of an image, made in the browser. The
    original is kept; the copy's ``parent`` is it."""
    if asset.kind != "image":
        raise LibraryError("Only pictures can be edited here.")
    ext = IMAGE_TYPES.get(getattr(upload, "content_type", ""))
    if ext is None:
        raise LibraryError("The edited picture must be JPEG, PNG or WebP.")
    if upload.size > EDIT_MAX_BYTES:
        raise LibraryError("The edited picture is over 30 MB.")
    data = upload.read()
    try:
        images.verify(data)
    except images.ImageError as exc:
        raise LibraryError(str(exc)) from exc
    url = _save(asset.account, "edit", ext, data)
    base = asset.name.rsplit(".", 1)[0] if asset.name else "picture"
    copy = record(
        asset.account,
        url,
        kind="image",
        mime=upload.content_type,
        name=f"{base} (edited).{ext}",
        data=data,
        source=MediaAsset.Source.EDIT,
        user=user,
        parent=asset,
        credit=asset.credit,
        credit_url=asset.credit_url,
    )
    copy.tags = list(asset.tags or [])
    copy.save(update_fields=["tags"])
    return copy


def cover_status_key(asset: MediaAsset) -> str:
    """Where the last cover attempt for ``asset`` is kept for the page to show."""
    return f"media-cover:{asset.account_id}:{asset.pk}"


def grab_cover(asset: MediaAsset, seconds: float, user=None) -> MediaAsset:
    """One frame of a video as a JPEG, for a Reel cover. Runs ffmpeg on the ``media``
    queue against our own media host only (SSRF)."""
    from .services import _is_own_media_url

    if asset.kind != "video":
        raise LibraryError("Choose a video to take a cover from.")
    if not _is_own_media_url(asset.url):
        raise LibraryError("The video must be stored in Akilent.")
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise LibraryError("ffmpeg isn't installed on this server.")
    seconds = max(0.0, float(seconds))
    if asset.duration and seconds > asset.duration:
        raise LibraryError("Choose a time inside the video.")
    out = subprocess.run(  # noqa: S603 - fixed binary, URL limited to our media host
        [
            binary,
            "-v",
            "error",
            "-ss",
            f"{seconds:.2f}",
            "-i",
            asset.url,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-f",
            "image2",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ],
        capture_output=True,
        timeout=COVER_TIMEOUT,
        check=False,
    )
    if out.returncode != 0 or not out.stdout:
        raise LibraryError("Couldn't take a frame at that time. Try another time.")
    data = out.stdout
    url = _save(asset.account, "cover", "jpg", data)
    base = asset.name.rsplit(".", 1)[0] if asset.name else "video"
    return record(
        asset.account,
        url,
        kind="image",
        mime="image/jpeg",
        name=f"{base} cover at {seconds:.1f}s.jpg",
        data=data,
        source=MediaAsset.Source.COVER,
        user=user,
        parent=asset,
    )


# --- Stock photos (Pexels) ---------------------------------------------------------------


def stock_available() -> bool:
    return bool(getattr(settings, "PEXELS_API_KEY", ""))


def _pexels(path: str, **params) -> dict:
    import requests

    resp = requests.get(
        f"{PEXELS_API}/{path}",
        params=params,
        headers={"Authorization": settings.PEXELS_API_KEY},
        timeout=15,
    )
    if resp.status_code != 200:
        raise LibraryError("Stock photos aren't available right now. Try again.")
    return resp.json()


def _photo(p: dict) -> dict:
    src = p.get("src") or {}
    return {
        "id": p.get("id"),
        "thumb": src.get("medium") or src.get("small") or "",
        "alt": p.get("alt") or "",
        "photographer": p.get("photographer") or "",
        "photographer_url": p.get("photographer_url") or "",
        "page_url": p.get("url") or "",
    }


def stock_search(query: str, page: int = 1) -> list[dict]:
    query = " ".join((query or "").split())[:100]
    if not query:
        return []
    data = _pexels("search", query=query, per_page=24, page=max(1, int(page)))
    return [_photo(p) for p in data.get("photos") or []]


def stock_import(account, photo_id, user) -> MediaAsset:
    """Copy one Pexels photo into the library with its credit. The photo is looked up
    by id, so the file and credit come from Pexels, never from the browser."""
    from urllib.parse import urlparse

    import requests

    try:
        photo_id = int(photo_id)
    except (TypeError, ValueError) as exc:
        raise LibraryError("Choose a photo.") from exc
    p = _pexels(f"photos/{photo_id}")
    src = (p.get("src") or {}).get("large2x") or (p.get("src") or {}).get("original")
    if not src or urlparse(src).hostname != PEXELS_IMAGE_HOST:
        raise LibraryError("That photo can't be used.")
    resp = requests.get(src, timeout=30, allow_redirects=False, stream=True)
    try:
        data, mime = images.download(resp, EDIT_MAX_BYTES, "The photo")
    except images.ImageError as exc:
        raise LibraryError(str(exc)) from exc
    url = _save(account, "stock", images.IMAGE_TYPES[mime], data)
    name = (p.get("alt") or f"Pexels photo {photo_id}")[:180]
    return record(
        account,
        url,
        kind="image",
        mime=mime,
        name=f"{name}.{images.IMAGE_TYPES[mime]}",
        data=data,
        source=MediaAsset.Source.STOCK,
        user=user,
        credit=f"Photo by {p.get('photographer') or 'a Pexels photographer'} on Pexels",
        credit_url=p.get("url") or "https://www.pexels.com",
    )


def picker_items(account, kind: str = "", q: str = "") -> list[dict]:
    qs = MediaAsset.objects.filter(account=account, archived_at__isnull=True)
    if kind in ("image", "video"):
        qs = qs.filter(kind=kind)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(tags__icontains=q))
    return [
        {
            "id": a.pk,
            "url": a.url,
            "name": a.name,
            "kind": a.kind,
            "width": a.width,
            "height": a.height,
            "credit": a.credit,
        }
        for a in qs[:60]
    ]
