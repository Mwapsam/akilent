"""What a post brought in, per channel: link clicks → chats → leads → orders → revenue.

Counts only explicit evidence: chats that used the channel's tracked link, or DMs a
private reply to a comment on it opened (apps.conversations.entry_points). Leads and
orders count when their attribution names the channel as its source. Nothing is
credited by timing, so these are lower bounds, and the card says so.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Count, Sum

from .links import short_url
from .models import ContentPost, ContentPostVariant, LinkClick, TrackedLink


def results_for(post: ContentPost) -> list[dict]:
    from apps.commerce.models import Order
    from apps.conversations.models import (
        ConversationAttribution,
        ConversationEntryPoint,
    )

    rows = []
    variants = post.variants.filter(
        platform__in=(
            ContentPostVariant.Platform.FACEBOOK,
            ContentPostVariant.Platform.INSTAGRAM,
        )
    ).exclude(status=ContentPostVariant.Status.CANCELLED)
    for variant in variants:
        link = TrackedLink.objects.filter(variant=variant).first()
        clicks = (
            LinkClick.objects.filter(link=link, is_bot=False).count() if link else 0
        )
        chats = (
            ConversationEntryPoint.objects.filter(variant=variant)
            .values("conversation")
            .distinct()
            .count()
        )
        if link is None and not chats:
            continue
        attributed = ConversationAttribution.objects.filter(source_variant=variant)
        revenue = [
            {"currency": r["currency"], "total": r["total"] or Decimal("0")}
            for r in Order.objects.filter(
                attribution__source_variant=variant, status=Order.Status.PAID
            )
            .values("currency")
            .annotate(total=Sum("total"), n=Count("id"))
            .order_by("currency")
        ]
        rows.append(
            {
                "variant": variant,
                "channel": variant.get_platform_display(),
                "link": link if link and link.disabled_at is None else None,
                "link_url": short_url(link)
                if link and link.disabled_at is None
                else "",
                "clicks": clicks,
                "chats": chats,
                "leads": attributed.filter(lead__isnull=False).count(),
                "orders": attributed.filter(order__isnull=False).count(),
                "revenue": revenue,
            }
        )
    return rows
