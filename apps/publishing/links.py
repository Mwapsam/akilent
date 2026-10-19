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


def caption_line(link: TrackedLink) -> str:
    return f"Chat with us on WhatsApp: {short_url(link)}"


def with_link_line(caption: str, link: TrackedLink) -> str:
    """The caption with the link line at the end, added once."""
    line = caption_line(link)
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
