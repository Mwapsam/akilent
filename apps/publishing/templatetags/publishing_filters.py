"""Template filters and tags for publishing app."""

import re

from django import template
from django.template.loader import render_to_string

register = template.Library()

# One tone per status, shared by posts and per-channel variants so a status looks the
# same on every Content Studio page.
STATUS_TONES = {
    "idea": "tone-neutral",
    "draft": "tone-neutral",
    "in_review": "tone-amber",
    "approved": "tone-teal",
    "scheduled": "tone-info",
    "publishing": "tone-amber",
    "processing": "tone-amber",
    "published": "tone-teal",
    "failed": "tone-coral",
    "cancelled": "tone-neutral",
    "reconciling": "tone-amber",
}

# Variant labels where the model's display name reads as jargon.
_VARIANT_LABELS = {"reconciling": "Needs check"}

_IMAGE_RE = re.compile(r"\.(jpe?g|png|webp|gif)(\?|#|$)", re.IGNORECASE)
_VIDEO_RE = re.compile(r"\.(mp4|mov|m4v|webm)(\?|#|$)", re.IGNORECASE)


def _badge(status, label):
    return render_to_string(
        "publishing/_status_badge.html",
        {"tone": STATUS_TONES.get(status, "tone-neutral"), "label": label},
    )


@register.simple_tag
def status_badge(status, label=""):
    """Status pill for a post or plan status: {% status_badge post.status post.get_status_display %}."""
    return _badge(status, label or str(status).replace("_", " ").capitalize())


@register.filter
def status_tone(status):
    """The tone class for a status, for chips that are not a badge (calendar entries)."""
    return STATUS_TONES.get(status, "tone-neutral")


@register.filter
def variant_status_badge(variant):
    """Status pill for a per-channel variant."""
    status = variant.status
    label = _VARIANT_LABELS.get(status) or variant.get_status_display()
    return _badge(status, label)


@register.simple_tag(takes_context=True)
def reconciling_count(context):
    """How many channel publications in the current workspace need a manual check."""
    from apps.accounts.utils import get_current_account
    from apps.publishing.models import ContentPostVariant

    account = context.get("account")
    request = context.get("request")
    if account is None and request is not None:
        account = get_current_account(request)
    if account is None:
        return 0
    return ContentPostVariant.objects.filter(
        post__account=account, status=ContentPostVariant.Status.RECONCILING
    ).count()


@register.filter
def media_kind(url):
    """'image', 'video' or 'file', judged by the URL's extension. media_urls stores no type."""
    url = str(url or "")
    if _IMAGE_RE.search(url):
        return "image"
    if _VIDEO_RE.search(url):
        return "video"
    return "file"


@register.filter
def first_media(post):
    """The post's first media URL, or ''."""
    urls = getattr(post, "media_urls", None) or []
    return urls[0] if urls else ""


@register.filter
def platform_display_name(platform):
    """Return user-friendly platform name."""
    names = {
        "facebook": "Facebook",
        "instagram": "Instagram",
        "email": "Email",
        "whatsapp": "WhatsApp",
    }
    return names.get(platform, platform.title())


@register.filter
def is_variant_retryable(variant):
    """Check if variant is eligible for retry."""
    from apps.publishing.models import ContentPostVariant

    # Only FAILED variants can be retried
    return variant.status == ContentPostVariant.Status.FAILED


@register.filter
def publication_url_for_variant(variant):
    """Return the published URL if available, else empty string."""
    return variant.publication_url or ""


@register.filter
def variant_error_message(variant):
    """Return human-readable error message for variant."""
    if not variant.last_error:
        return ""

    error_type = variant.last_error_type
    error_msg = variant.last_error

    # Add context based on error type
    if error_type == "timeout":
        return f"{error_msg} (ambiguous - may be in provider, requires reconciliation)"
    if error_type == "permission":
        return f"{error_msg} (permission issue - check account connection)"
    if error_type == "rate_limit":
        return f"{error_msg} (rate limited - retry after delay)"
    if error_type == "provider_outage":
        return f"{error_msg} (provider unavailable - will retry)"
    if error_type == "validation":
        return f"{error_msg} (validation failed - fix and resubmit)"
    return error_msg


@register.filter
def variant_performance(variant):
    """The Performance card's rows for a published channel (see metrics.performance_for)."""
    from apps.publishing.metrics import performance_for

    return performance_for(variant)


@register.filter
def published_channels(post):
    """The post's published Facebook/Instagram channels, which have performance numbers."""
    from apps.publishing.models import ContentPostVariant

    return [
        v
        for v in post.variants.all()
        if v.status == ContentPostVariant.Status.PUBLISHED
        and v.publication_id
        and v.platform
        in (ContentPostVariant.Platform.FACEBOOK, ContentPostVariant.Platform.INSTAGRAM)
    ]


@register.filter
def join_platforms(variants):
    names = [v.get_platform_display() for v in variants]
    return " and ".join(names)


@register.filter
def reach_summary(post):
    """'Reach: Instagram 1,204 · Facebook 340' from each channel's latest read; '' if none."""
    parts = []
    for v in post.variants.all():
        value = ((v.metrics_latest or {}).get("values") or {}).get("reach")
        if value is not None:
            parts.append(f"{v.get_platform_display()} {value:,}")
    return f"Reach: {' · '.join(parts)}" if parts else ""
