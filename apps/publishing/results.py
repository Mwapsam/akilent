"""What a post brought in, per channel: link clicks → chats → leads → orders → revenue.

Counts only explicit evidence: chats that used the channel's tracked link, or DMs a
private reply to a comment on it opened (apps.conversations.entry_points). Leads and
orders count when their attribution names the channel as its source, which only an
explicitly named conversation can do. Nothing is credited by timing or by the
recent-conversation guess, so these are lower bounds, and the card says so.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from django.db.models import Count, Exists, OuterRef, Q, Sum

from .links import short_url
from .models import (
    BioPage,
    BioTile,
    ContentPost,
    ContentPostVariant,
    LinkClick,
    TrackedLink,
)


def post_credit_q(prefix: str = "attribution__") -> Q:
    """Orders or leads a post is credited with. Only an explicitly named conversation can
    credit a post (the database constraint ``attribution_guess_credits_nothing``), so this
    is simply "names a post"; it exists so every report spells the rule the same way."""
    return Q(**{f"{prefix}source_post__isnull": False})


def results_for(post: ContentPost) -> list[dict]:
    from apps.commerce.models import Order
    from apps.conversations.models import (
        ConversationAttribution,
        ConversationEntryPoint,
    )

    from . import bio

    bio_on = bio.is_released()
    bio_page = BioPage.objects.filter(account_id=post.account_id).first()
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
                # Instagram only: link chats split by how sure each credit is.
                "credit_breakdown": bio.instagram_breakdown(variant),
                "bio": _bio_state(bio_on, bio_page, variant, link),
            }
        )
    return rows


def _bio_state(bio_on: bool, page, variant, link) -> dict | None:
    """Whether this Instagram channel is on the bio page, for the card's button."""
    if not bio_on or variant.platform != ContentPostVariant.Platform.INSTAGRAM:
        return None
    tile = (
        BioTile.objects.filter(page=page, link=link).first() if page and link else None
    )
    return {
        "page": page,
        "on_page": bool(tile and tile.archived_at is None),
    }


# The channels Content Studio publishes to; posts on them are what post results are about.
PUBLISHING_CHANNELS = frozenset(
    {ContentPostVariant.Platform.FACEBOOK, ContentPostVariant.Platform.INSTAGRAM}
)


def publishing_channels(account) -> frozenset:
    """The publishing channels this business can use now: switched on, and Content Studio
    usable. Empty means post results don't apply to it."""
    from apps.billing import api as billing_api
    from apps.core.channels import enabled_channels

    on = PUBLISHING_CHANNELS & enabled_channels()
    return on if on and billing_api.usable(account, "content_studio") else frozenset()


def post_tracking_available(account) -> bool:
    """The same gate for the Insights journey and the "where this came from" block on
    leads and orders: the business can publish posts at all."""
    return bool(publishing_channels(account))


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

    publishing = publishing_channels(account)
    if not publishing:
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
    # A post's entry points only: a WhatsApp campaign's are Campaigns' to report.
    entry_points = ConversationEntryPoint.objects.filter(
        account=account, post__isnull=False
    )
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

    # One source for chats from posts: each chat started from a post in the period is
    # credited to the first post it came through, so the posts list adds up to the count
    # (a customer who tapped two posts' links is one chat, not two).
    first_post: dict[int, int] = {}
    chat_people: dict[int, int] = {}
    for conversation_id, post_id, contact_id in (
        entry_points.filter(**during("matched_at"))
        .order_by("matched_at", "pk")
        .values_list("conversation_id", "post_id", "conversation__contact_id")
    ):
        if conversation_id not in first_post:
            first_post[conversation_id] = post_id
            chat_people[conversation_id] = contact_id
    out["chats"] = len(first_post)
    from_posts = post_credit_q()
    # Leads and orders from posts are counted as people from those chats, so the funnel
    # only narrows (chats → leads → orders; apps.conversations.reporting.narrowing).
    from apps.conversations.reporting import narrowing

    lead_people = set(
        Lead.objects.filter(
            from_posts, account=account, **during("created_at")
        ).values_list("contact_id", flat=True)
    )
    order_people = set(
        Order.objects.filter(
            from_posts, account=account, **during("created_at")
        ).values_list("contact_id", flat=True)
    )
    _, out["leads"], out["orders"] = narrowing(
        [set(chat_people.values()), lead_people, order_people]
    )
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
    # Bounded by the period, not the account's whole history: conversations with an
    # inbound message in the period, minus any that already had one before it.
    inbound = Message.objects.filter(
        account=account, direction=Message.Direction.INBOUND
    )
    out["untracked_chats"] = (
        Conversation.objects.filter(
            account=account,
            pk__in=inbound.filter(**during("timestamp")).values("conversation"),
        )
        # Never linked to a post (a chat a WhatsApp campaign started still counts here).
        .exclude(entry_points__post__isnull=False)
        .exclude(
            Exists(
                inbound.filter(conversation=OuterRef("pk"), timestamp__lt=period.start)
            )
        )
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
    per_post = Counter(first_post.values())
    posts = ContentPost.objects.in_bulk(list(per_post))
    for pk, post in posts.items():
        # The template reads post.chats (an annotation before this was counted in Python).
        setattr(post, "chats", per_post[pk])  # noqa: B010
    out["top"] = sorted(posts.values(), key=lambda p: (-per_post[p.pk], -p.pk))[:5]
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
