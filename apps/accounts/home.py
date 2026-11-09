"""Home's panels beyond the work queue (2026-11-09 Dashboard review, step 2).

- ``this_week``: four compact numbers for the last 7 days with a trend line each, from the same
  reporting rules as Insights (so Home and Insights never disagree);
- ``recent_activity``: what actually happened (new chats, leads, paid orders, posts published,
  campaigns sent), newest first, instead of email sends only;
- ``coming_up``: scheduled campaign sends and scheduled posts in one list.
"""

from __future__ import annotations

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

ACTIVITY_DAYS = 14


def _spark(values) -> list:
    """A trend line only when there's something to draw: two or more readings and at least
    one that isn't zero (a flat line of zeros is noise, not a trend)."""
    numbers = [v for v in values if v is not None]
    return numbers if len(numbers) > 1 and any(numbers) else []


def this_week(account, now=None) -> dict:
    """``{"items": [{"label", "value", "kind", "spark", "href"}]}`` for the last 7 days.

    ``kind`` says how to show ``value``: "count" or "duration" (seconds)."""
    from apps.conversations import reporting

    now = now or timezone.now()
    period = reporting.last_days(7, now)
    rows = reporting.first_replies(account, period.start, period.end)
    head = reporting.headline(account, period, rows, now=now)
    trend = reporting.trend(account, period, rows, now=now)
    zone = reporting.business_zone(account)
    first_day = timezone.localtime(period.start, zone).date()
    per_day = [0] * len(trend["labels"])
    for row in rows:
        i = (timezone.localtime(row["first_in"], zone).date() - first_day).days
        if 0 <= i < len(per_day):
            per_day[i] += 1
    insights = reverse("insights")
    return {
        "items": [
            {
                "label": "Conversations",
                "value": len(rows),
                "kind": "count",
                "spark": _spark(per_day),
                "href": f"{insights}?period=7&tab=response",
            },
            {
                "label": "First reply",
                "value": head["reply"]["value"],
                "kind": "duration",
                "spark": _spark(trend["reply_minutes"]),
                "href": f"{insights}?period=7&tab=response",
            },
            {
                "label": "Leads",
                "value": head["leads"]["value"],
                "kind": "count",
                "spark": _spark(trend["leads"]),
                "href": f"{insights}?period=7&tab=sales",
            },
            {
                "label": "Paid orders",
                "value": head["paid"]["value"],
                "kind": "count",
                "spark": _spark(trend["paid"]),
                "href": f"{insights}?period=7&tab=sales",
            },
        ]
    }


def recent_activity(account, now=None, limit: int = 8) -> list[dict]:
    """``[{"when", "icon", "text", "url"}]``, newest first, from the last ACTIVITY_DAYS."""
    from apps.commerce.models import Order
    from apps.conversations.attention import short_name
    from apps.conversations.models import Conversation
    from apps.core.channels import hide_disabled_channels
    from apps.crm.models import Lead
    from apps.email.models import BulkEmailCampaign
    from apps.publishing.models import ContentPostVariant
    from apps.whatsapp.models.campaign import WhatsAppCampaign

    now = now or timezone.now()
    since = now - timedelta(days=ACTIVITY_DAYS)
    channels = dict(Conversation.Channel.choices)
    items: list[dict] = []

    for c in (
        hide_disabled_channels(
            Conversation.objects.filter(account=account, created_at__gte=since)
        )
        .select_related("contact")
        .order_by("-created_at")[:limit]
    ):
        items.append(
            {
                "when": c.created_at,
                "icon": "message-circle",
                "text": f"{short_name(c.contact)} wrote on {channels.get(c.channel, c.channel)}",
                "url": reverse("conversations:detail", args=[c.public_id]),
            }
        )
    for lead in (
        Lead.objects.filter(account=account, created_at__gte=since)
        .select_related("contact")
        .order_by("-created_at")[:limit]
    ):
        items.append(
            {
                "when": lead.created_at,
                "icon": "tag",
                "text": f"New lead · {short_name(lead.contact)}",
                "url": reverse("crm:sales"),
            }
        )
    for order in (
        Order.objects.filter(
            account=account, status=Order.Status.PAID, paid_at__gte=since
        )
        .select_related("contact")
        .order_by("-paid_at")[:limit]
    ):
        amount = f"{order.currency} {order.total:,.2f}".strip()
        items.append(
            {
                "when": order.paid_at,
                "icon": "check-circle",
                "text": f"Order paid · {amount} · {short_name(order.contact)}",
                "url": reverse("commerce:orders"),
            }
        )
    for variant in (
        ContentPostVariant.objects.filter(
            post__account=account,
            status=ContentPostVariant.Status.PUBLISHED,
            published_at__gte=since,
        )
        .select_related("post")
        .order_by("-published_at")[:limit]
    ):
        caption = (getattr(variant.post, "caption", "") or "").strip()
        short = (caption[:40] + "…") if len(caption) > 40 else caption
        items.append(
            {
                "when": variant.published_at,
                "icon": "image",
                "text": f"Post published on {variant.get_platform_display()}"
                + (f": “{short}”" if short else ""),
                "url": reverse("publishing:post-detail", args=[variant.post_id]),
            }
        )
    for campaign in WhatsAppCampaign.objects.filter(
        account=account, started_at__gte=since
    ).order_by("-started_at")[:limit]:
        items.append(
            {
                "when": campaign.started_at,
                "icon": "megaphone",
                "text": f"Campaign sent · {campaign.name}",
                "url": "/campaigns/",
            }
        )
    for bulk in (
        BulkEmailCampaign.objects.filter(account=account, completed_at__gte=since)
        .select_related("template")
        .order_by("-completed_at")[:limit]
    ):
        name = bulk.subject_override or (bulk.template.name if bulk.template else "")
        items.append(
            {
                "when": bulk.completed_at,
                "icon": "megaphone",
                "text": "Email campaign sent" + (f" · {name}" if name else ""),
                "url": "/email/campaigns/",
            }
        )
    items.sort(key=lambda i: i["when"], reverse=True)
    return items[:limit]


def coming_up(
    account, upcoming_sends: list[dict], now=None, limit: int = 5
) -> list[dict]:
    """Scheduled sends (``views._upcoming_sends``) and scheduled posts, soonest first:
    ``[{"when", "label", "channel", "url"}]``."""
    from apps.publishing.models import ContentPostVariant

    now = now or timezone.now()
    items = [dict(s, url=s.get("url", "")) for s in upcoming_sends]
    for variant in (
        ContentPostVariant.objects.filter(
            post__account=account,
            status=ContentPostVariant.Status.SCHEDULED,
            scheduled_at__gte=now,
        )
        .select_related("post")
        .order_by("scheduled_at")[:limit]
    ):
        caption = (getattr(variant.post, "caption", "") or "").strip()
        items.append(
            {
                "when": variant.scheduled_at,
                "label": (caption[:60] + "…")
                if len(caption) > 60
                else caption or "Post",
                "channel": variant.platform,
                "url": reverse("publishing:post-detail", args=[variant.post_id]),
            }
        )
    items.sort(key=lambda i: i["when"])
    return items[:limit]
