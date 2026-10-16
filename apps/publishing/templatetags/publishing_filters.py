"""Template filters and tags for publishing app."""

from django import template

register = template.Library()


@register.filter
def variant_status_badge(variant):
    """Return HTML badge showing variant status with appropriate styling."""
    status = variant.status

    status_colors = {
        "published": ("tone-teal", "Published"),
        "failed": ("tone-coral", "Failed"),
        "scheduled": ("tone-neutral", "Scheduled"),
        "publishing": ("tone-amber", "Publishing"),
        "reconciling": ("tone-yellow", "Reconciliation"),
        "cancelled": ("tone-neutral", "Cancelled"),
        "draft": ("tone-neutral", "Draft"),
    }

    color, display = status_colors.get(status, ("tone-neutral", status.title()))

    return f'<span class="text-sm px-2 py-0.5 rounded-full {color}">{display}</span>'


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


@register.simple_tag
def variant_status_icon(variant):
    """Return icon HTML for variant status."""
    status = variant.status

    icons = {
        "published": "✓",
        "failed": "✗",
        "scheduled": "⏱",
        "publishing": "⏳",
        "reconciling": "❓",
        "cancelled": "⊘",
        "draft": "✎",
    }

    return icons.get(status, "•")
