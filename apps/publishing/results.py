"""What a post brought in, per channel: link clicks → chats → leads → orders → revenue.

Counts only explicit evidence: chats that used the channel's tracked link, or DMs a
private reply to a comment on it opened (apps.conversations.entry_points). Leads and
orders count when their attribution names the channel as its source *and* the team named
the conversation (method EXPLICIT). Nothing is credited by timing or by the
recent-conversation guess, so these are lower bounds, and the card says so.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db.models import Count, Min, Q, Sum

from .links import short_url
from .models import ContentPost, ContentPostVariant, LinkClick, TrackedLink


def _explicit():
    from apps.conversations.models import ConversationAttribution

    return ConversationAttribution.Method.EXPLICIT


def post_credit_q(prefix: str = "attribution__") -> Q:
    """Orders or leads a post is credited with: explicit attribution naming a post.

    The method is checked here, not only when the record is written, so an older
    recent-conversation row that kept a ``source_post`` is never read as a post's sale.
    """
    return Q(**{f"{prefix}method": _explicit(), f"{prefix}source_post__isnull": False})


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
        attributed = ConversationAttribution.objects.filter(
            post_credit_q(""), source_variant=variant
        )
        revenue = [
            {"currency": r["currency"], "total": r["total"] or Decimal("0")}
            for r in Order.objects.filter(
                post_credit_q(),
                attribution__source_variant=variant,
                status=Order.Status.PAID,
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


def post_tracking_available(account) -> bool:
    """Whether this business can publish posts at all, so post results make sense to it:
    a publishing channel is on and Content Studio is usable. The same gate for the
    Insights journey and the "where this came from" block on leads and orders."""
    from apps.billing import api as billing_api
    from apps.core.channels import enabled_channels

    return bool({"instagram", "facebook"} & enabled_channels()) and billing_api.usable(
        account, "content_studio"
    )


def journey(account, period) -> dict | None:
    """The whole customer journey for one period: posts published → link clicks → chats
    from posts → leads → orders → paid revenue (per currency, never summed across them).

    Same evidence as ``results_for``, across every post. A step with no way to measure it
    yet (no chat link on any post, no chat started from one) is ``None``, shown as "Not
    tracked yet" rather than a 0 that would read as "it didn't work". None when the
    business can't publish at all.
    """
    from apps.commerce.models import Order
    from apps.conversations.models import ConversationEntryPoint
    from apps.core.channels import enabled_channels
    from apps.crm.models import Lead

    publishing = {"instagram", "facebook"} & enabled_channels()
    if not post_tracking_available(account):
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

    out: dict[str, Any] = {
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
        # What no post is credited with (see below); None, like the steps above, until
        # chats from posts can be tracked at all.
        "untracked_chats": None,
        "guessed_revenue": None,
        "untracked_revenue": None,
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
    from_posts = post_credit_q()
    out["leads"] = Lead.objects.filter(
        from_posts, account=account, **during("created_at")
    ).count()
    out["orders"] = Order.objects.filter(
        from_posts, account=account, **during("created_at")
    ).count()
    all_paid = Order.objects.filter(
        account=account, status=Order.Status.PAID, **during("paid_at")
    )
    paid = all_paid.filter(from_posts)
    out["revenue"] = [
        {"currency": r["currency"], "total": r["total"] or Decimal("0")}
        for r in paid.values("currency")
        .annotate(total=Sum("total"))
        .order_by("currency")
    ]
    # The side no post is credited with, kept visible rather than dropped. Chats and
    # revenue are separate measures: a chat no post started may have no order, and an
    # order may have no chat at all.
    from apps.conversations.models import Conversation, ConversationAttribution, Message

    # Both chat counts are chats *started* in the period, so they never overlap and side
    # by side they mean the same thing: ``chats`` started from a post (matched in the
    # period), ``untracked_chats`` started with the customer's first message in the period
    # and never linked to a post. A customer who came from a post long ago and writes
    # again now started their chat earlier, so they are in neither.
    out["untracked_chats"] = (
        Conversation.objects.filter(account=account, entry_points__isnull=True)
        .annotate(
            first_in=Min(
                "messages__timestamp",
                filter=Q(messages__direction=Message.Direction.INBOUND),
            )
        )
        .filter(**during("first_in"))
        .count()
    )
    guessed = Q(attribution__method=ConversationAttribution.Method.RECENT_CONVERSATION)
    # Three disjoint buckets of paid revenue, each per currency: credited to a post
    # (``revenue``), linked to a chat only by the recent-chat guess, and everything else.
    out["guessed_revenue"] = _per_currency(all_paid.filter(guessed))
    out["untracked_revenue"] = _per_currency(
        all_paid.exclude(from_posts).exclude(guessed)
    )
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


def _per_currency(orders) -> list[dict]:
    return [
        {
            "currency": r["currency"],
            "orders": r["orders"],
            "total": r["total"] or Decimal("0"),
        }
        for r in orders.values("currency")
        .annotate(orders=Count("id"), total=Sum("total"))
        .order_by("currency")
    ]
