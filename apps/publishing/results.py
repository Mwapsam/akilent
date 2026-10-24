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


def journey(account, period) -> dict | None:
    """The whole customer journey for one period: posts published → link clicks → chats
    from posts → leads → orders → paid revenue (per currency, never summed across them).

    Same evidence as ``results_for``, across every post. A step with no way to measure it
    yet (no chat link on any post, no chat started from one) is ``None``, shown as "Not
    tracked yet" rather than a 0 that would read as "it didn't work". None when the
    business can't publish at all.
    """
    from apps.billing import api as billing_api
    from apps.commerce.models import Order
    from apps.conversations.models import ConversationEntryPoint
    from apps.core.channels import enabled_channels
    from apps.crm.models import Lead

    publishing = {"instagram", "facebook"} & enabled_channels()
    if not publishing or not billing_api.usable(account, "content_studio"):
        return None

    in_period = {"__gte": period.start, "__lt": period.end}

    def during(field):
        return {field + k: v for k, v in in_period.items()}

    published = ContentPostVariant.objects.filter(
        post__account=account,
        platform__in=publishing,
        status=ContentPostVariant.Status.PUBLISHED,
        **during("published_at"),
    )
    has_links = TrackedLink.objects.filter(account=account).exists()
    entry_points = ConversationEntryPoint.objects.filter(account=account)
    chats_tracked = has_links or entry_points.exists()

    out = {
        "posts": published.values("post").distinct().count(),
        "clicks": LinkClick.objects.filter(
            link__account=account, is_bot=False, **during("clicked_at")
        ).count()
        if has_links
        else None,
        "chats": None,
        "leads": None,
        "orders": None,
        "revenue": None,
        "top": [],
        # Chat links open WhatsApp: the hint for an untracked step says how to start.
        "chat_links": "whatsapp" in enabled_channels(),
    }
    if not chats_tracked:
        return out

    out["chats"] = (
        entry_points.filter(**during("matched_at"))
        .values("conversation")
        .distinct()
        .count()
    )
    from_posts = {"attribution__source_post__isnull": False}
    out["leads"] = Lead.objects.filter(
        account=account, **from_posts, **during("created_at")
    ).count()
    out["orders"] = Order.objects.filter(
        account=account, **from_posts, **during("created_at")
    ).count()
    paid = Order.objects.filter(
        account=account, status=Order.Status.PAID, **from_posts, **during("paid_at")
    )
    out["revenue"] = [
        {"currency": r["currency"], "total": r["total"] or Decimal("0")}
        for r in paid.values("currency")
        .annotate(total=Sum("total"))
        .order_by("currency")
    ]
    # The posts behind the numbers, so each one opens its own results card.
    out["top"] = list(
        ContentPost.objects.filter(
            account=account,
            entry_points__matched_at__gte=period.start,
            entry_points__matched_at__lt=period.end,
        )
        .annotate(chats=Count("entry_points__conversation", distinct=True))
        .order_by("-chats", "-pk")[:5]
    )
    return out
