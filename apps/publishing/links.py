"""Tracked WhatsApp chat links for posts.

Each Facebook or Instagram channel of a post can carry its own short link,
``{SITE_URL}/go/<code>``. Opening it counts a click and sends the visitor to
``wa.me/<business number>`` with a prefilled message that ends in "(ref CODE)". The
inbound side (apps.conversations.entry_points) spots the code and links that chat
to the post and channel.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from .models import ContentPost, ContentPostVariant, TrackedLink

# Crockford base32 without I, L, O, U: easy to read aloud and hard to mistype.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 6
# The marker a customer's first message carries. Matched case-insensitively, and
# tolerant of "ref:" or extra spaces a phone keyboard may add.
REF_RE = re.compile(r"\(\s*ref[:\s]\s*([0-9A-Za-z]{6})\s*\)", re.IGNORECASE)

DEFAULT_PREFILL = "Hi! I saw your post and I'd like to know more."

_BOT_MARKERS = (
    "bot",
    "crawler",
    "spider",
    "facebookexternalhit",
    "facebot",
    "whatsapp",  # link preview fetcher
    "preview",
    "slurp",
    "curl",
    "python-requests",
)


class NoWhatsAppNumber(Exception):
    """The workspace has no connected WhatsApp number for links to open."""


def new_code() -> str:
    while True:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
        if not TrackedLink.objects.filter(code=code).exists():
            return code


def whatsapp_number_for(account) -> str:
    """Digits of the workspace's ready WhatsApp number, or '' if it has none."""
    for number in account.whatsapp_numbers.filter(is_active=True):
        digits = re.sub(r"\D", "", number.display_number or "")
        if number.is_ready and digits:
            return digits
    return ""


def link_for_variant(variant: ContentPostVariant) -> TrackedLink:
    """The channel's tracked link, created (or re-enabled) on first use."""
    post = variant.post
    existing = TrackedLink.objects.filter(variant=variant).first()
    if existing is not None:
        if existing.disabled_at is not None:
            existing.disabled_at = None
            existing.save(update_fields=["disabled_at"])
        return existing
    number = whatsapp_number_for(post.account)
    if not number:
        raise NoWhatsAppNumber
    return TrackedLink.objects.create(
        account=post.account,
        post=post,
        variant=variant,
        campaign=post.plan_item.campaign if post.plan_item else None,
        code=new_code(),
        wa_number=number,
        prefill_text=DEFAULT_PREFILL,
    )


def disable_link(variant: ContentPostVariant) -> None:
    TrackedLink.objects.filter(variant=variant, disabled_at__isnull=True).update(
        disabled_at=timezone.now()
    )


def active_link(variant: ContentPostVariant) -> TrackedLink | None:
    return TrackedLink.objects.filter(variant=variant, disabled_at__isnull=True).first()


def short_url(link: TrackedLink) -> str:
    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return f"{base}/go/{link.code}"


def whatsapp_url(link: TrackedLink) -> str:
    text = f"{link.prefill_text or DEFAULT_PREFILL} (ref {link.code})"
    return f"https://wa.me/{link.wa_number}?text={quote(text)}"


def caption_line(link: TrackedLink, text: str = "") -> str:
    return f"{text or DEFAULT_WHATSAPP_TEXT}: {short_url(link)}"


def with_link_line(caption: str, link: TrackedLink, text: str = "") -> str:
    """The caption with the link line at the end, added once (matched on the URL, so
    a changed CTA text never leaves two lines)."""
    if short_url(link) in caption:
        return caption
    return f"{caption.rstrip()}\n\n{caption_line(link, text)}".strip()


# --- Call to action -------------------------------------------------------------

CTA_NONE = "none"
CTA_WHATSAPP = "whatsapp"
CTA_LINK = "link"
CTA_KINDS = (CTA_NONE, CTA_WHATSAPP, CTA_LINK)

DEFAULT_WHATSAPP_TEXT = "Chat with us on WhatsApp"
DEFAULT_LINK_TEXT = "Shop now"
CTA_TEXT_MAX = 80
PREFILL_MAX = 200  # TrackedLink.prefill_text
# Instagram captions can't hold a tappable link, so the line points to the bio.
INSTAGRAM_SUFFIX = " (link in bio)"


class CtaError(ValueError):
    """The call to action in the composer form isn't valid."""


def parse_cta(data) -> dict:
    """The CTA from the composer form, cleaned. Raises CtaError on a bad value.

    A form without ``cta_kind`` (an older form or an API-style post) is read from the
    WhatsApp link checkboxes, so ticking one still means "Chat on WhatsApp".
    """
    kind = (data.get("cta_kind") or "").strip()
    if not kind:
        wants_link = data.get("wa_link_facebook") or data.get("wa_link_instagram")
        kind = CTA_WHATSAPP if wants_link else CTA_NONE
    if kind not in CTA_KINDS:
        raise CtaError("Choose a call to action.")
    if kind == CTA_NONE:
        return {}

    text = " ".join((data.get("cta_text") or "").split())
    if len(text) > CTA_TEXT_MAX:
        raise CtaError(f"Keep the call to action under {CTA_TEXT_MAX} characters.")
    if kind == CTA_WHATSAPP:
        prefill = " ".join((data.get("cta_prefill") or "").split())
        if len(prefill) > PREFILL_MAX:
            raise CtaError(
                f"Keep the customer's first message under {PREFILL_MAX} characters."
            )
        return {
            "kind": kind,
            "text": text or DEFAULT_WHATSAPP_TEXT,
            "prefill": prefill or DEFAULT_PREFILL,
        }

    from django.core.exceptions import ValidationError
    from django.core.validators import URLValidator

    url = (data.get("cta_url") or "").strip()
    if not url:
        raise CtaError("Add the link people should visit.")
    try:
        URLValidator(schemes=["https"])(url)
    except ValidationError:
        raise CtaError(
            "Use a full link that starts with https://, like https://yourshop.com."
        ) from None
    return {"kind": kind, "text": text or DEFAULT_LINK_TEXT, "url": url}


def cta_for(post: ContentPost) -> dict:
    """The post's CTA. A post saved before CTAs existed that has a WhatsApp link reads
    as a WhatsApp CTA with the default wording."""
    if post.cta:
        return post.cta
    if post.pk and links_for_post(post):
        return {
            "kind": CTA_WHATSAPP,
            "text": DEFAULT_WHATSAPP_TEXT,
            "prefill": DEFAULT_PREFILL,
        }
    return {}


def cta_caption(caption: str, cta: dict, platform: str, link=None) -> str:
    """``caption`` with the CTA line for ``platform`` at the end, added once.

    Facebook gets a tappable URL. Instagram gets the wording only, pointing to the bio.
    A WhatsApp CTA needs that channel's tracked ``link``; without one nothing is added.
    """
    kind = (cta or {}).get("kind")
    text = cta.get("text", "") if cta else ""
    if kind == CTA_WHATSAPP:
        if link is None:
            return caption
        if platform == ContentPostVariant.Platform.FACEBOOK:
            return with_link_line(caption, link, text)
        line = f"{text or DEFAULT_WHATSAPP_TEXT}{INSTAGRAM_SUFFIX}"
    elif kind == CTA_LINK:
        url = cta.get("url", "")
        if platform == ContentPostVariant.Platform.FACEBOOK:
            if url and url in caption:
                return caption
            line = f"{text or DEFAULT_LINK_TEXT}: {url}"
        else:
            line = f"{text or DEFAULT_LINK_TEXT}{INSTAGRAM_SUFFIX}"
    else:
        return caption
    if line in caption:
        return caption
    return f"{caption.rstrip()}\n\n{line}".strip()


def is_bot(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    return not ua or any(marker in ua for marker in _BOT_MARKERS)


def visitor_hash(ip: str, user_agent: str) -> str:
    """Salted and rotated daily, so it counts distinct visitors without tracking anyone."""
    day = timezone.now().date().isoformat()
    raw = f"{settings.SECRET_KEY}:{day}:{ip}:{user_agent}"
    return hashlib.sha256(raw.encode()).hexdigest()


def find_code(text: str) -> str:
    match = REF_RE.search(text or "")
    return match.group(1).upper() if match else ""


def links_for_post(post: ContentPost) -> dict[str, TrackedLink]:
    links = TrackedLink.objects.filter(
        post=post, disabled_at__isnull=True, variant__isnull=False
    ).select_related("variant")
    return {link.variant.platform: link for link in links if link.variant}
