"""Read-only evidence for the staging journey check (docs/STAGING-VALIDATION-CHECKLIST.md).

    python manage.py journey_evidence --account <slug> --post <post id> [--days 30]

Prints JSON: the post and its approval trail, each channel's state, the tracked link and
its clicks, every chat that reached the workspace in the window, attributions, leads,
orders and payments, the alert markers, and the reports (post card, journey, revenue,
sales). It changes nothing and is safe to paste into the checklist:

- no access tokens, signing secrets or provider payloads;
- no message text;
- phone numbers keep only their last 4 digits;
- payment transaction ids are kept (the payment provider shows them), card data never exists here.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.publishing.bio import credit_basis


def _mask_phone(phone: str | None) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return f"…{digits[-4:]}" if digits else ""


def _subject_id(attribution) -> str | None:
    """The lead, order or deal an attribution is about (the model allows exactly one)."""
    subject = attribution.lead or attribution.order or attribution.deal
    return subject.public_id if subject is not None else None


def _iso(value):
    return value.isoformat() if value else None


def _jsonable(value):
    if isinstance(value, Decimal):
        # Money, printed the same on every database (SQLite drops trailing zeros on sums).
        return f"{value:.2f}"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "pk"):
        return getattr(value, "public_id", None) or value.pk
    raise TypeError(type(value))


class Command(BaseCommand):
    help = "Print sanitized evidence of one post's journey for staging verification (read-only)"

    def add_arguments(self, parser):
        parser.add_argument("--account", required=True, help="Workspace slug")
        parser.add_argument("--post", required=True, type=int, help="ContentPost id")
        parser.add_argument("--days", type=int, default=30, help="Reporting window")

    def handle(self, *args, **opts):
        from apps.accounts.models import Account
        from apps.commerce.models import Order
        from apps.conversations import reporting
        from apps.conversations.models import (
            Conversation,
            ConversationAttribution,
            Message,
        )
        from apps.crm.models import Lead
        from apps.publishing.alerts import ALERTS_KEY
        from apps.publishing.models import ContentPost, LinkClick, TrackedLink
        from apps.publishing.results import journey, results_for

        account = Account.objects.filter(slug=opts["account"]).first()
        if account is None:
            raise CommandError(f"No workspace with slug {opts['account']!r}.")
        # Scoped to the workspace: another business's post id is simply not found.
        post = ContentPost.objects.filter(pk=opts["post"], account=account).first()
        if post is None:
            raise CommandError("No such post in this workspace.")
        now = timezone.now()
        since = now - timedelta(days=opts["days"])
        period = reporting.last_days(opts["days"], now)

        # Records are picked by the same events the reports count, so every number in
        # ``reports`` can be traced to a row below: orders created *or paid* in the window
        # (revenue is counted by paid_at), the attributions of the listed leads and orders
        # (whenever they were written), and every conversation behind any of them.
        orders = Order.objects.filter(
            Q(created_at__gte=since) | Q(paid_at__gte=since), account=account
        )
        leads = Lead.objects.filter(account=account, created_at__gte=since)
        attributions = ConversationAttribution.objects.filter(
            Q(lead__in=leads) | Q(order__in=orders), account=account
        )
        conversations = Conversation.objects.filter(
            Q(messages__timestamp__gte=since)
            | Q(entry_points__matched_at__gte=since)
            | Q(pk__in=orders.values("conversation"))
            | Q(pk__in=leads.values("conversation")),
            account=account,
        ).distinct()
        sales = reporting.sales(account, period)
        out = {
            "generated_at": _iso(now),
            "windows": {
                "days": opts["days"],
                "since": _iso(since),
                "post_card": "all time (as on the post page)",
                "journey_and_revenue": "the window, by event time (matched_at, created_at, paid_at)",
            },
            "workspace": {"slug": account.slug, "id": account.pk},
            "post": {
                "id": post.pk,
                "public_id": post.public_id,
                "status": post.status,
                "approval_status": post.approval_status,
                "approval_valid": post.is_approval_valid(),
                "content_hash_now": post.compute_content_hash(),
                "ai_assisted": post.ai_assisted,
                "approval_trail": [
                    {
                        "action": log.action,
                        "by_user_id": log.performed_by_id,
                        "content_hash": log.content_hash,
                        "at": _iso(log.created_at),
                    }
                    for log in post.approval_logs.order_by("created_at")
                ],
            },
            "channels": [
                {
                    "id": v.pk,
                    "platform": v.platform,
                    "status": v.status,
                    "publication_id": v.publication_id,
                    "published_at": _iso(v.published_at),
                    "last_error_type": v.last_error_type,
                    "last_error": (v.last_error or "")[:200],
                    "alerts_sent": (v.metadata or {}).get(ALERTS_KEY, {}),
                }
                for v in post.variants.order_by("platform")
            ],
            "tracked_links": [
                {
                    "code": link.code,
                    "platform": link.variant.platform if link.variant else None,
                    "disabled": link.disabled_at is not None,
                    "clicks": LinkClick.objects.filter(link=link, is_bot=False).count(),
                    "bot_clicks": LinkClick.objects.filter(
                        link=link, is_bot=True
                    ).count(),
                }
                for link in TrackedLink.objects.filter(post=post).select_related(
                    "variant"
                )
            ],
            "conversations": [
                {
                    "public_id": c.public_id,
                    "channel": c.channel,
                    "customer": _mask_phone(c.contact.phone if c.contact else None),
                    "inbound_messages": Message.objects.filter(
                        conversation=c, direction=Message.Direction.INBOUND
                    ).count(),
                    "entry_points": [
                        {
                            "kind": e.kind,
                            "post_id": e.post_id,
                            "variant_id": e.variant_id,
                            "bio_tile_id": e.bio_tile_id,
                            "credit_basis": credit_basis(e),
                            "matched_at": _iso(e.matched_at),
                        }
                        for e in c.entry_points.order_by("matched_at")
                    ],
                }
                for c in conversations.select_related("contact").order_by("pk")
            ],
            "attributions": [
                {
                    "subject": "lead"
                    if a.lead_id
                    else "order"
                    if a.order_id
                    else "deal",
                    "subject_id": _subject_id(a),
                    "conversation": a.conversation.public_id,
                    "method": a.method,
                    "credits_post": a.credits_post,
                    "credit_basis": credit_basis(a.entry_point)
                    if a.credits_post
                    else None,
                    "source_post_id": a.source_post_id,
                    "at": _iso(a.attributed_at),
                }
                for a in attributions.select_related(
                    "lead", "order", "deal", "conversation", "entry_point"
                )
            ],
            "leads": [
                {
                    "public_id": lead.public_id,
                    "status": lead.status,
                    "conversation": lead.conversation.public_id
                    if lead.conversation
                    else None,
                }
                for lead in leads.select_related("conversation")
            ],
            "orders": [
                {
                    "public_id": o.public_id,
                    "status": o.status,
                    "currency": o.currency,
                    "total": o.total,
                    "paid_at": _iso(o.paid_at),
                    "conversation": o.conversation.public_id
                    if o.conversation
                    else None,
                    "payments": [
                        {
                            "status": p.status,
                            "provider": p.provider,
                            "transaction_id": p.transaction_id,
                        }
                        for p in o.payments.order_by("created_at")
                    ],
                }
                for o in orders.select_related("conversation")
            ],
            "reports": {
                "post_card": [
                    {k: v for k, v in row.items() if k not in ("variant", "link")}
                    for row in results_for(post)
                ],
                "journey": {
                    k: v
                    for k, v in (journey(account, period) or {}).items()
                    if k != "top"
                },
                "revenue": reporting.revenue(account, period),
                "sales_by_method": {
                    k: sales[k] for k in ("paid_orders", "named", "recent")
                },
            },
        }
        self.stdout.write(json.dumps(out, indent=2, default=_jsonable))
