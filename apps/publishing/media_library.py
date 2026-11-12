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
import re
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


_SIZE_CACHE_SECONDS = 24 * 3600
_SIZE_UNKNOWN_CACHE_SECONDS = 300
_SIZE_READ_MAX_BYTES = 8 * 1024 * 1024


def image_size(account, url: str) -> tuple[int, int] | None:
    """A picture's (width, height): from the media library when it knows, else read from
    the file's header on our own media host and remembered for a day. None when unknown
    (someone else's host, unreadable, or not a picture)."""
    from django.core.cache import cache

    if not url:
        return None
    asset = MediaAsset.objects.filter(
        account=account, url_digest=url_digest(url)
    ).first()
    if asset is not None and asset.width and asset.height:
        return asset.width, asset.height

    from .services import _is_own_media_url

    if not _is_own_media_url(url):
        return None
    key = f"publishing:image-size:{url_digest(url)}"
    cached = cache.get(key)
    if cached is not None:
        return tuple(cached) if cached else None
    size = _read_image_size(url)
    cache.set(
        key,
        list(size) if size else [],
        _SIZE_CACHE_SECONDS if size else _SIZE_UNKNOWN_CACHE_SECONDS,
    )
    if size and asset is not None:
        MediaAsset.objects.filter(pk=asset.pk).update(width=size[0], height=size[1])
    return size


def _read_image_size(url: str) -> tuple[int, int] | None:
    """Read just enough of the file for its size: a JPEG or PNG says it in the first
    few kilobytes."""
    import requests
    from PIL import ImageFile

    parser = ImageFile.Parser()
    read = 0
    try:
        with requests.get(url, timeout=5, stream=True, allow_redirects=False) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(16 * 1024):
                parser.feed(chunk)
                read += len(chunk)
                if parser.image is not None:
                    w, h = parser.image.size
                    return (w, h) if w and h else None
                if read > _SIZE_READ_MAX_BYTES:
                    return None
    except Exception as exc:
        logger.debug("image_size: could not read %s: %s", url, exc)
    return None


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
            asset, created = MediaAsset.objects.get_or_create(
                account=account, url_digest=url_digest(url), defaults=values
            )
    except IntegrityError:
        asset = MediaAsset.objects.get(account=account, url_digest=url_digest(url))
        created = False
    if created:
        # Its thumbnail and any size the upload didn't give, made in the background.
        queue_details(asset)
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


def needed_ids(account, assets) -> set[int]:
    """Files on the page that a draft, scheduled or publishing post uses, so ``remove``
    would refuse them: the card says so instead of offering Remove. One query (and its
    prefetch), whatever the page size."""
    urls = {a.url.lower(): a.pk for a in assets if getattr(a, "used_in", 1)}
    if not urls:
        return set()
    match = Q()
    for url in urls:
        match |= Q(media_urls__icontains=url) | Q(variants__options__icontains=url)
    posts = (
        ContentPost.objects.filter(account=account)
        .filter(match)
        .filter(
            Q(status__in=LIVE_POST_STATUSES)
            | Q(variants__status__in=LIVE_VARIANT_STATUSES)
        )
        .distinct()
        .only("pk", "media_urls")
        .prefetch_related(
            Prefetch(
                "variants",
                queryset=ContentPostVariant.objects.only("post_id", "options"),
            )
        )
    )
    found: set[int] = set()
    for post in posts:
        text = json.dumps(
            [post.media_urls or [], [v.options or {} for v in post.variants.all()]],
            ensure_ascii=False,
        ).lower()
        found.update(pk for url, pk in urls.items() if url in text)
    return found


def _strings(value, out: set[str]) -> None:
    if isinstance(value, str):
        out.add(value.lower())
    elif isinstance(value, dict):
        for v in value.values():
            _strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _strings(v, out)


def usage_counts_all(account) -> dict[int, int]:
    """``usage_counts`` for every file of the business, for sorting by use and the Unused
    filter: two queries whatever the library's size. A post counts once per file, matched
    on the URL exactly (as media or as any option, a Reel cover among them)."""
    urls: dict[str, list[int]] = {}
    for pk, url in MediaAsset.objects.filter(account=account).values_list("pk", "url"):
        urls.setdefault(url.lower(), []).append(pk)
    if not urls:
        return {}
    posts = (
        ContentPost.objects.filter(account=account)
        .only("pk", "media_urls")
        .prefetch_related(
            Prefetch(
                "variants",
                queryset=ContentPostVariant.objects.only("post_id", "options"),
            )
        )
    )
    counts: dict[int, int] = {}
    for post in posts:
        found: set[str] = set()
        _strings(post.media_urls or [], found)
        for v in post.variants.all():
            _strings(v.options or {}, found)
        for url in found & urls.keys():
            for pk in urls[url]:
                counts[pk] = counts.get(pk, 0) + 1
    return counts


# A copy's name as phones and computers save it: "WhatsApp Image 2026-09-10 at 14.22.01",
# "Screenshot 2026-09-10 at 14.22.01", "Screenshot_20260910-142201", "IMG_20260910_142201".
_CAMERA_NAME = re.compile(
    r"^(?P<what>whatsapp (?:image|video)|screenshot|screen shot|img|vid|pxl)"
    r"[ _-]*(?P<y>20\d\d)-?(?P<m>\d\d)-?(?P<d>\d\d)(?!\d).*$",
    re.IGNORECASE,
)
_LABELS = {
    "whatsapp image": "WhatsApp image",
    "whatsapp video": "WhatsApp video",
    "screenshot": "Screenshot",
    "screen shot": "Screenshot",
    "img": "Photo",
    "pxl": "Photo",
    "vid": "Video",
}


_STORAGE_SUFFIX = re.compile(
    r"_(?=[A-Za-z0-9]{7}$)(?![A-Z][a-z]+[0-9]*$)(?=.*[A-Z])(?=.*[a-z0-9])[A-Za-z0-9]{7}$"
)


def display_name(name: str) -> str:
    """A file's name for the grid: no extension, underscores as spaces, and a phone's
    "WhatsApp Image 2026-09-10 at 14.22.01" as "WhatsApp image · 10 Sep". The full name
    stays on the file and in its details."""
    import datetime

    base = (name or "").strip()
    stem, dot, ext = base.rpartition(".")
    if dot and stem and 1 <= len(ext) <= 5 and ext.isalnum():
        base = stem
    # Storage's "_K2nNF6v" on a second upload of the same name: seven random letters and
    # digits mixing cases, so a real "summer1" or "Summer1" stays.
    base = _STORAGE_SUFFIX.sub("", base) or base
    match = _CAMERA_NAME.match(base)
    if match:
        try:
            day = datetime.date(int(match["y"]), int(match["m"]), int(match["d"]))
        except ValueError:
            day = None
        if day:
            label = _LABELS[" ".join(match["what"].lower().split())]
            return f"{label} · {day.day} {day:%b}"
    return " ".join(base.replace("_", " ").split()) or "Untitled"


def _plain_name(name: str) -> str:
    """A name with its extension and a copy's " (1)" or " copy" left off, for spotting
    the same file uploaded twice."""
    base = (name or "").rsplit(".", 1)[0]
    # A storage suffix ("_Sv1jm0R") marks the same name uploaded again, like " (1)".
    base = (_STORAGE_SUFFIX.sub("", base) or base).lower()
    base = re.sub(r"(\s*\(\d+\)|\s+copy(\s*\d+)?)+$", "", base)
    return " ".join(base.replace("_", " ").split())


def duplicates(account, assets) -> dict[int, MediaAsset]:
    """For each file on the page that looks like an older file of the business, that
    older file: the same content (sha256), or the same size and name once a copy's
    " (1)" is left off. One query."""
    shas = {a.sha256 for a in assets if a.sha256}
    sizes = {a.size_bytes for a in assets if a.size_bytes}
    if not shas and not sizes:
        return {}
    others = list(
        MediaAsset.objects.filter(account=account, archived_at__isnull=True)
        .filter(Q(sha256__in=shas) | Q(size_bytes__in=sizes))
        .only("pk", "name", "sha256", "size_bytes", "created_at")
        .order_by("created_at", "pk")
    )
    found: dict[int, MediaAsset] = {}
    for a in assets:
        for o in others:
            if (o.created_at, o.pk) >= (a.created_at, a.pk):
                break
            same = (a.sha256 and o.sha256 == a.sha256) or (
                a.size_bytes
                and o.size_bytes == a.size_bytes
                and _plain_name(o.name) == _plain_name(a.name)
            )
            if same:
                found[a.pk] = o
                break
    return found


def similar(
    account, name: str, size: int | None, sha256: str = ""
) -> MediaAsset | None:
    """A live file of the business that a file about to be uploaded looks like: the same
    content, or the same size and name once a copy's " (1)" is left off."""
    live = MediaAsset.objects.filter(account=account, archived_at__isnull=True)
    if sha256:
        found = live.filter(sha256=sha256).first()
        if found:
            return found
    if not size:
        return None
    plain = _plain_name(name)
    for other in live.filter(size_bytes=size).only("pk", "name", "url", "kind")[:20]:
        if _plain_name(other.name) == plain:
            return other
    return None


def date_group(created_at, today) -> str:
    """The label a file is grouped under in the newest-first grid."""
    day = timezone.localtime(created_at).date()
    if (today - day).days < 7:
        return "This week"
    if (day.year, day.month) == (today.year, today.month):
        return "Earlier this month"
    if day.year == today.year:
        return f"{day:%B}"
    return f"{day:%B %Y}"


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


def remove_preview(asset: MediaAsset) -> tuple[str, list]:
    """What ``remove`` would do, without doing it, for the confirm that says so."""
    blocking = list(blocking_posts(asset)[:5])
    if blocking:
        return "blocked", blocking
    used = list(usage(asset)[:5])
    return ("archived", used) if used else ("deleted", [])


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


def _parse_tags(raw: str) -> list[str]:
    tags: list[str] = []
    for t in (raw or "").replace("#", "").split(","):
        t = " ".join(t.split()).lower()[:TAG_MAX]
        if t and t not in tags:
            tags.append(t)
    return tags


def set_tags(asset: MediaAsset, raw: str) -> None:
    asset.tags = _parse_tags(raw)[:TAGS_MAX]
    asset.save(update_fields=["tags"])


def bulk(account, ids, action: str, raw_tags: str = "") -> dict[str, int]:
    """One action on several files of this business: ``tag`` (adds tags), ``remove``
    (each by the one rule: blocked, archived or deleted) or ``restore``. Returns how many
    files ended each way, for the message."""
    assets = list(MediaAsset.objects.filter(account=account, pk__in=list(ids)[:200]))
    done: dict[str, int] = {}
    if action == "tag":
        new = _parse_tags(raw_tags)
        if not new:
            return done
        for asset in assets:
            asset.tags = list(dict.fromkeys([*(asset.tags or []), *new]))[:TAGS_MAX]
            asset.save(update_fields=["tags"])
        done["tagged"] = len(assets)
    elif action == "remove":
        for asset in assets:
            if asset.archived_at:
                continue
            outcome, _ = remove(asset)
            done[outcome] = done.get(outcome, 0) + 1
    elif action == "archive":
        # Hidden from the library, the file kept: safe whatever uses it, and undoable.
        done["archived"] = MediaAsset.objects.filter(
            pk__in=[a.pk for a in assets], archived_at__isnull=True
        ).update(archived_at=timezone.now())
    elif action == "restore":
        done["restored"] = MediaAsset.objects.filter(
            pk__in=[a.pk for a in assets], archived_at__isnull=False
        ).update(archived_at=None)
    return done


# --- Thumbnails and missing facts ---------------------------------------------------------

THUMB_BOX = (600, 750)  # 4:5, two pixels per point for a ~300px tile
_DETAILS_MAX_BYTES = 40 * 1024 * 1024


def make_thumbnail(data: bytes) -> bytes | None:
    """A WebP no bigger than THUMB_BOX, upright (EXIF), or None if it isn't a picture."""
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as src:
            img: Image.Image = ImageOps.exif_transpose(src) or src
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
            img.thumbnail(THUMB_BOX, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, "WEBP", quality=80, method=4)
            return out.getvalue()
    except Exception:
        return None


def fill_details(asset: MediaAsset) -> list[str]:
    """Fill what a file is missing: its size, a picture's dimensions and thumbnail. Reads
    the file from our own media host only. Returns the fields it set."""
    import requests

    from .services import _is_own_media_url

    wants_size = not asset.size_bytes
    wants_picture = asset.kind == "image" and (
        not asset.thumb_url or not asset.width or not asset.height
    )
    if not (wants_size or wants_picture) or not _is_own_media_url(asset.url):
        return []
    changed: dict = {}
    try:
        if not wants_picture:
            # A size is all that's missing (a video, say): ask, don't download.
            head = requests.head(asset.url, timeout=10, allow_redirects=False)
            length = int(head.headers.get("Content-Length") or 0)
            if head.ok and length:
                changed["size_bytes"] = length
        else:
            with requests.get(
                asset.url, timeout=30, stream=True, allow_redirects=False
            ) as resp:
                resp.raise_for_status()
                body = bytearray()
                for chunk in resp.iter_content(256 * 1024):
                    body.extend(chunk)
                    if len(body) > _DETAILS_MAX_BYTES:
                        return []
            data = bytes(body)
            if wants_size:
                changed["size_bytes"] = len(data)
            if not asset.width or not asset.height:
                w, h = _dimensions(data)
                if w and h:
                    changed["width"], changed["height"] = w, h
            if not asset.thumb_url and (thumb := make_thumbnail(data)):
                changed["thumb_url"] = _save(asset.account, "thumbs", "webp", thumb)
    except Exception as exc:
        logger.info("fill_details: couldn't read asset %s: %s", asset.pk, exc)
        return []
    if changed:
        MediaAsset.objects.filter(pk=asset.pk).update(**changed)
        for k, v in changed.items():
            setattr(asset, k, v)
    return list(changed)


def queue_details(asset: MediaAsset) -> None:
    """Fill a new file's facts after the upload commits, off the request."""
    if asset.size_bytes and (asset.kind != "image" or asset.thumb_url):
        return
    from functools import partial

    from .tasks import fill_asset_details

    transaction.on_commit(partial(fill_asset_details.delay, asset.pk), robust=True)


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
