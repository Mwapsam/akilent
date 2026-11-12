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
def post_headline(post):
    """A post's name on its own page: the caption's first sentence (or first line), cut at
    a word near 70 characters, never mid-word."""
    import re

    for line in (post.caption or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # First sentence: up to the first . ! ? followed by a space (keeps "e.g." and URLs).
        m = re.match(r"(.+?[.!?])(\s|$)", line)
        if m and len(m.group(1)) >= 12:
            line = m.group(1)
        if len(line) <= 70:
            return line
        cut = line[:70].rsplit(" ", 1)[0].rstrip(" ,;:—-")
        return (cut or line[:69]) + "…"
    return "Untitled post"


@register.filter
def platform_names(platforms) -> str:
    """A post's channels as people read them: "Instagram, Facebook"."""
    return ", ".join(platform_display_name(p) for p in platforms or [])


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
    error_msg = (variant.last_error or "").strip()
    if not error_msg:
        return ""

    error_type = variant.last_error_type
    # The provider's own sentence, ended once: "failed!" stays as it is, "(code 190)" gets a
    # full stop, and nothing ends up with "!." or "..".
    said = error_msg if error_msg.endswith((".", "!", "?")) else error_msg + "."

    # What happened, in the customer's words, and the next step. Nothing here promises an
    # automatic retry: a failed channel stays failed until someone retries it (a provider
    # outage is saved as FAILED too, in services.py).
    channel = platform_display_name(variant.platform or "") or "the channel"
    starts = channel[:1].upper() + channel[1:]  # the same name opening a sentence
    if error_type == "timeout":
        return (
            f"{said} {starts} didn't answer in time, so it may already be live. "
            f"Check it on {channel} before trying again."
        )
    if error_type == "permission":
        return f"{said} Reconnect {channel} in Channels, then try again."
    if error_type == "rate_limit":
        return f"{said} {starts} asked us to slow down. Try again in a few minutes."
    if error_type == "provider_outage":
        return f"{said} {starts} had a problem on its side. Try again later."
    if error_type == "validation":
        return f"{said} Change the post to fix this, then try again."
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
    """'Reach: Instagram 1,204 · Facebook 340' from each live channel's latest read; a
    channel without a number says why ('not read yet', 'needs permission', '—') instead of
    dropping out, so a post on both channels never looks Facebook-only. '' if none is live."""
    from apps.publishing.metrics import State

    short = {
        State.NEEDS_PERMISSION: "needs permission",
        State.PENDING: "not read yet",
        State.UNSUPPORTED: "not available",
        State.BELOW_THRESHOLD: "too few to show",
    }
    parts = []
    for v in post.variants.all():
        if v.status != "published" or v.platform not in ("facebook", "instagram"):
            continue
        latest = v.metrics_latest or {}
        value = (latest.get("values") or {}).get("reach")
        if value is not None:
            parts.append(f"{v.get_platform_display()} {value:,}")
        elif not v.metrics_collected_at:
            parts.append(f"{v.get_platform_display()} not read yet")
        else:
            state = (latest.get("states") or {}).get("reach")
            parts.append(f"{v.get_platform_display()} {short.get(state or '', '—')}")
    return f"Reach: {' · '.join(parts)}" if parts else ""


@register.filter
def platform_labels(platforms):
    """["instagram", "facebook"] -> "Instagram, Facebook"."""
    from apps.publishing.composer import PLATFORM_LABELS

    return ", ".join(PLATFORM_LABELS.get(p, p.capitalize()) for p in platforms or [])


@register.simple_tag
def post_tracking_on(account) -> bool:
    """Whether a lead or order page should say where it came from in terms of posts."""
    from apps.publishing.results import post_tracking_available

    return account is not None and post_tracking_available(account)


@register.filter
def credit_label(entry) -> str:
    """How sure a post's credit from this entry point is ("" when confirmed); see
    apps.publishing.bio.credit_basis."""
    from apps.publishing.bio import credit_label as label

    return label(entry) if entry is not None else ""


# The tab each Content Studio page belongs to (the same rules as _subnav.html's active tab).
_STUDIO_TABS = (
    (
        ("/publishing/calendar", "/publishing/slots", "/publishing/recurring"),
        "Calendar",
    ),
    (("/publishing/library", "/publishing/import"), "Library"),
    (("/publishing/planning",), "Planning"),
    (("/publishing/channels",), "Channels"),
    (("/publishing/media",), "Media"),
    (("/publishing/analytics",), "Analytics"),
    (("/publishing/sales",), "Sales"),
    (("/publishing/ai",), "AI"),
    (("/publishing/bio",), "Bio page"),
    (("/publishing/reconciliation",), "Needs check"),
)


@register.simple_tag(takes_context=True)
def studio_tab(context) -> str:
    """The page's title: its tab's name, or "" on pages outside the tabs (which keep "Content
    Studio" as their title)."""
    request = context.get("request")
    path = getattr(request, "path", "")
    if path == "/publishing/":
        return "Overview"
    for prefixes, label in _STUDIO_TABS:
        if path.startswith(prefixes):
            return label
    return ""


@register.filter
def to_json(value) -> str:
    """A value as JSON for an Alpine attribute (x-data). Not marked safe on purpose: the
    template escapes its quotes for the attribute, and the browser unescapes them."""
    import json

    return json.dumps(value if value is not None else [])
