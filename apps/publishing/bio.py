"""The link-in-bio page (``/b/<slug>``) and what its tiles mean for post credit.

An Instagram bio holds one link. Before this page existed, a business put one post's
chat link there, so every chat from the bio was credited to that post, whichever post
the customer had actually seen. Each tile here carries its own ref code, so a chat
that starts from a tile names the tile's post.

Instagram link credits are labelled at read time; no record is ever rewritten
(``credit_basis``):

- through a bio-page tile: confirmed for that post;
- no bio page yet, or before it went live: "via bio link, post not confirmed";
- after it went live, through the post's own link (pasted into the bio, a story
  sticker): "direct link: confirmed for the link, not proven to be from this post".

Facebook links sit in the post's own caption and comment replies are tied to the post,
so those credits stay confirmed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from . import images
from .links import new_code
from .models import BioPage, BioTile, ContentPostVariant, TrackedLink

logger = logging.getLogger(__name__)

FLAG = "cs_bio_page"
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
LABEL_MAX = 80

CONFIRMED = "confirmed"
BIO_UNCONFIRMED = "bio_unconfirmed"
DIRECT_LINK = "direct_link"
LABELS = {
    CONFIRMED: "",
    BIO_UNCONFIRMED: "via bio link, post not confirmed",
    DIRECT_LINK: "direct link: confirmed for the link, not proven to be from this post",
}


class BioError(ValueError):
    """The bio page or tile request isn't valid; the message is for the owner."""


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def page_url(page: BioPage) -> str:
    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return f"{base}/b/{page.slug}"


def suggested_slug(account) -> str:
    base = slugify(account.company_name or "")[:36].strip("-") or "shop"
    if len(base) < 3:
        base = f"{base}-shop"
    slug, n = base, 2
    while BioPage.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def create_page(account, slug: str, title: str) -> BioPage:
    """Create the page and put it live (``activated_at`` is set now)."""
    slug = (slug or "").strip().lower()
    title = " ".join((title or "").split())[:100]
    if not SLUG_RE.match(slug):
        raise BioError(
            "Use 3 to 40 lowercase letters, numbers and hyphens, starting and ending "
            "with a letter or number."
        )
    if not title:
        raise BioError("Give the page a title.")
    if BioPage.objects.filter(slug=slug).exists():
        raise BioError("That address is taken. Try another.")
    return BioPage.objects.create(
        account=account, slug=slug, title=title, activated_at=timezone.now()
    )


def set_active(page: BioPage, active: bool) -> None:
    """Turn the page on or off. Each time it's off is kept, because the Instagram bio
    can't be proven to point at the page then."""
    now = timezone.now()
    fields = ["is_active", "updated_at"]
    if active and page.activated_at is None:
        page.activated_at = now
        fields.append("activated_at")
    elif page.activated_at is not None and active != page.is_active:
        periods = [list(p) for p in page.off_periods or []]
        if not active:
            periods.append([now.isoformat(), None])
        elif periods and periods[-1][1] is None:
            periods[-1][1] = now.isoformat()
        page.off_periods = periods
        fields.append("off_periods")
    page.is_active = active
    page.save(update_fields=fields)


def was_off(page: BioPage, when) -> bool:
    """Whether the page was turned off at ``when``."""
    for off, on in page.off_periods or []:
        start = datetime.fromisoformat(off)
        if when >= start and (on is None or when < datetime.fromisoformat(on)):
            return True
    return False


def active_tiles(page: BioPage):
    return page.tiles.filter(archived_at__isnull=True).select_related(
        "link__post", "link__variant"
    )


def default_label(link: TrackedLink) -> str:
    caption = " ".join((link.post.caption or "").split())
    if len(caption) > LABEL_MAX:
        caption = caption[: LABEL_MAX - 1].rstrip() + "…"
    return caption or "Chat with us on WhatsApp"


@transaction.atomic
def add_tile(page: BioPage, link: TrackedLink, label: str = "") -> BioTile:
    """Put ``link``'s post on the page (restoring an archived tile), then archive the
    oldest unpinned tiles beyond ``max_tiles``. Archived tiles keep their code."""
    if link.account_id != page.account_id:
        raise BioError("That link belongs to another workspace.")
    if link.disabled_at is not None:
        raise BioError("This post's chat link is turned off.")
    label = " ".join((label or "").split())[:LABEL_MAX] or default_label(link)
    tile = BioTile.objects.filter(page=page, link=link).first()
    if tile is None:
        tile = BioTile.objects.create(
            page=page, link=link, code=new_code(), label=label
        )
    elif tile.archived_at is not None:
        tile.archived_at = None
        tile.created_at = timezone.now()  # restarts its time on the page
        tile.save(update_fields=["archived_at", "created_at"])
    enforce_limit(page, keep=tile)
    return tile


def enforce_limit(page: BioPage, keep: BioTile | None = None) -> list[BioTile]:
    """Archive the oldest unpinned tiles beyond ``max_tiles`` (never ``keep``)."""
    active = list(page.tiles.filter(archived_at__isnull=True).order_by("created_at"))
    over = len(active) - page.max_tiles
    archived = []
    for tile in active:
        if over <= 0:
            break
        if tile.pinned or (keep is not None and tile.pk == keep.pk):
            continue
        tile.archived_at = timezone.now()
        tile.save(update_fields=["archived_at"])
        archived.append(tile)
        over -= 1
    return archived


def archive_stale_tiles(now=None) -> int:
    """Archive unpinned tiles older than their page's ``archive_after_days``."""
    now = now or timezone.now()
    count = 0
    for page in BioPage.objects.all():
        cutoff = now - timedelta(days=page.archive_after_days)
        count += page.tiles.filter(
            archived_at__isnull=True, pinned=False, created_at__lt=cutoff
        ).update(archived_at=now)
    return count


@transaction.atomic
def move_tile(tile: BioTile, delta: int) -> bool:
    """Move a tile one place up (-1) or down (+1) among the tiles shown. Pinned tiles
    stay above the rest, so a tile only swaps with one in its own group."""
    tiles = list(active_tiles(tile.page).select_for_update(of=("self",)))
    for position, t in enumerate(tiles, 1):
        if t.position != position:
            t.position = position
            t.save(update_fields=["position"])
    i = next((n for n, t in enumerate(tiles) if t.pk == tile.pk), None)
    if i is None:
        return False
    j = i + delta
    if not 0 <= j < len(tiles) or tiles[j].pinned != tiles[i].pinned:
        return False
    a, b = tiles[i], tiles[j]
    a.position, b.position = b.position, a.position
    a.save(update_fields=["position"])
    b.save(update_fields=["position"])
    return True


def tile_stats(page: BioPage, since) -> dict[int, dict]:
    """Per tile since ``since``: people's clicks (bots left out) and chats started."""
    from django.db.models import Count, Q

    from apps.conversations.models import ConversationEntryPoint

    clicks = dict(
        page.tiles.annotate(
            n=Count(
                "clicks",
                filter=Q(clicks__is_bot=False, clicks__clicked_at__gte=since),
            )
        ).values_list("pk", "n")
    )
    chats = dict(
        ConversationEntryPoint.objects.filter(
            bio_tile__page=page, matched_at__gte=since
        )
        .values("bio_tile")
        .annotate(n=Count("conversation", distinct=True))
        .values_list("bio_tile", "n")
    )
    return {pk: {"clicks": n, "chats": chats.get(pk, 0)} for pk, n in clicks.items()}


# --- Look ----------------------------------------------------------------------------

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
AVATAR_TYPES = images.IMAGE_TYPES
AVATAR_MAX_BYTES = 2 * 1024 * 1024
# Pure black: with it, black or white is always at least 4.58:1 on any colour.
DARK_TEXT, LIGHT_TEXT = "#000000", "#ffffff"


def _luminance(hex_color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def text_on(hex_color: str) -> str:
    """Dark or white text, whichever reads better on ``hex_color``. One of the two is
    always at least 4.5:1 (WCAG AA), so the owner can pick any colour."""
    if not COLOR_RE.match(hex_color or ""):
        return ""
    if contrast(hex_color, DARK_TEXT) >= contrast(hex_color, LIGHT_TEXT):
        return DARK_TEXT
    return LIGHT_TEXT


def clean_color(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not COLOR_RE.match(value):
        raise BioError("Use a colour like #1f7a5c.")
    return value.lower()


def save_avatar(page: BioPage, upload) -> str:
    """Store an uploaded picture for the page and return its URL."""
    import uuid

    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    ext = AVATAR_TYPES.get(getattr(upload, "content_type", ""))
    if ext is None:
        raise BioError("Use a JPEG, PNG or WebP picture.")
    if upload.size > AVATAR_MAX_BYTES:
        raise BioError("Use a picture under 2 MB.")
    data = upload.read()
    try:
        images.verify(data)
    except images.ImageError as exc:
        raise BioError("That file isn't a picture we can read.") from exc
    path = default_storage.save(
        f"publishing/{page.account_id}/bio/{uuid.uuid4().hex}.{ext}", ContentFile(data)
    )
    return default_storage.url(path)


# --- Credit labels -------------------------------------------------------------------


def _platform(entry) -> str:
    variant = entry.variant or (entry.link.variant if entry.link_id else None)
    return variant.platform if variant else ""


_UNREAD: Any = (
    object()
)  # ``page`` not passed: read it (None means "there's no bio page")


def credit_basis(entry, page: BioPage | None = _UNREAD) -> str:
    """How sure a post's credit from ``entry`` (a ConversationEntryPoint) is.

    Pass ``page`` (the workspace's BioPage, or None when it has none) when labelling many
    entries of one workspace; otherwise it is read.
    """
    from apps.conversations.models import ConversationEntryPoint

    if entry is None or entry.kind != ConversationEntryPoint.Kind.TRACKED_LINK:
        return CONFIRMED
    if _platform(entry) != ContentPostVariant.Platform.INSTAGRAM:
        return CONFIRMED
    if entry.bio_tile_id:
        return CONFIRMED
    if page is _UNREAD:
        page = BioPage.objects.filter(account_id=entry.account_id).first()
    if (
        page is None
        or page.activated_at is None
        or entry.matched_at < page.activated_at
        or was_off(page, entry.matched_at)
    ):
        return BIO_UNCONFIRMED
    return DIRECT_LINK


def credit_label(entry, page: BioPage | None = _UNREAD) -> str:
    return LABELS[credit_basis(entry, page)]


def instagram_breakdown(variant: ContentPostVariant) -> dict | None:
    """For an Instagram channel: its link chats split by how sure the credit is."""
    from apps.conversations.models import ConversationEntryPoint

    if variant.platform != ContentPostVariant.Platform.INSTAGRAM:
        return None
    entries = ConversationEntryPoint.objects.filter(
        variant=variant, kind=ConversationEntryPoint.Kind.TRACKED_LINK
    ).select_related("variant", "link__variant")
    page = BioPage.objects.filter(account_id=variant.post.account_id).first()
    counts = {CONFIRMED: 0, BIO_UNCONFIRMED: 0, DIRECT_LINK: 0}
    seen = set()
    for e in entries:
        if e.conversation_id in seen:
            continue
        seen.add(e.conversation_id)
        counts[credit_basis(e, page)] += 1
    return counts if sum(counts.values()) else None


# --- Is the bio link set? -------------------------------------------------------------


def _normalise(url: str) -> str:
    url = (url or "").strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.rstrip("/")


def bio_link_set(page: BioPage) -> bool | None:
    """Whether the Instagram profile's website is this page. None: not checked yet."""
    if page.ig_website_checked_at is None:
        return None
    return _normalise(page.ig_website) == _normalise(page_url(page))


def check_instagram_website(page: BioPage) -> bool:
    """Read the profile's website field (``instagram_business_basic``) and store it.

    Returns False when it couldn't be read; the last good reading is kept.
    """
    ig = page.account.instagram_accounts.filter(is_active=True).first()
    if ig is None or not ig.access_token or ig.token_expired:
        return False
    try:
        resp = requests.get(
            f"https://graph.instagram.com/{settings.INSTAGRAM_GRAPH_VERSION}/me",
            params={"fields": "website", "access_token": ig.access_token},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.info(
                "Bio link check failed for page=%s: HTTP %s", page.pk, resp.status_code
            )
            return False
        website = (resp.json() or {}).get("website") or ""
    except (requests.RequestException, ValueError):
        logger.info("Bio link check failed for page=%s", page.pk, exc_info=True)
        return False
    page.ig_website = website[:500]
    page.ig_website_checked_at = timezone.now()
    page.save(update_fields=["ig_website", "ig_website_checked_at", "updated_at"])
    return True
