"""Explain why comment-to-DM (comment triggers) isn't firing.

    python manage.py instagram_check_comments

Read-only. For each connected Instagram account it shows which webhook fields
Meta has the account subscribed to, the comment webhooks received lately, the
active comment rules, and what happened to recent private replies. Never prints
a token.
"""

from __future__ import annotations

from datetime import timedelta

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from apps.instagram.models import InstagramBusinessAccount
from apps.instagram.models.comment import CommentThread
from apps.instagram.models.message import OutboundMessage
from apps.instagram.models.trigger import CommentTrigger
from apps.instagram.models.webhook import WebhookEventLog

GRAPH_IG = "https://graph.instagram.com"


class Command(BaseCommand):
    help = "Diagnose Instagram comment-to-DM (comment triggers)."

    def handle(self, *args, **options):
        out = self.stdout.write
        since = timezone.now() - timedelta(days=7)
        version = getattr(settings, "INSTAGRAM_GRAPH_VERSION", "v21.0")

        from apps.whatsapp.tasks import _automation_events_enabled

        out(f"Automation events enabled (site switch): {_automation_events_enabled()}")

        out("\nWebhooks received in the last 7 days, by type and status:")
        rows = (
            WebhookEventLog.objects.filter(received_at__gte=since)
            .values("event_type", "status")
            .annotate(n=Count("id"))
            .order_by("event_type", "status")
        )
        for row in rows:
            out(f"  {row['event_type']:<8} {row['status']:<10} {row['n']}")
        for event in WebhookEventLog.objects.filter(
            event_type__in=["comment", "mention"]
        ).order_by("-pk")[:5]:
            entry = (event.raw_payload.get("entry") or [{}])[0]
            value = ((entry.get("changes") or [{}])[0]).get("value") or {}
            out(
                f"  #{event.pk} {event.status} entry_id={entry.get('id')} "
                f"account_pk={event.instagram_account_id} "
                f"from={(value.get('from') or {}).get('id')} "
                f"text={(value.get('text') or '')[:30]!r} {event.error_message[:120]}"
            )

        for iba in InstagramBusinessAccount.objects.filter(is_active=True):
            out(
                f"\n@{iba.username} (pk={iba.pk}, ig_id={iba.instagram_business_account_id})"
            )
            try:
                resp = requests.get(
                    f"{GRAPH_IG}/{version}/{iba.instagram_business_account_id}/subscribed_apps",
                    params={"access_token": iba.access_token or ""},
                    timeout=10,
                )
                out(f"  Meta subscription: HTTP {resp.status_code} {resp.text[:300]}")
            except requests.RequestException as exc:
                out(f"  Meta subscription: request failed: {exc}")

            triggers = CommentTrigger.objects.filter(account=iba.account)
            out(
                f"  Comment rules: {triggers.filter(is_active=True).count()} active / {triggers.count()} total"
            )
            for t in triggers.order_by("priority"):
                out(
                    f"    - {t.name!r} active={t.is_active} match={t.match_type} "
                    f"keywords={t.keywords!r}"
                )

            out("  Latest comment threads:")
            for thread in CommentThread.objects.filter(instagram_account=iba).order_by(
                "-pk"
            )[:5]:
                out(
                    f"    #{thread.pk} {thread.received_at:%Y-%m-%d %H:%M} "
                    f"text={thread.body[:30]!r} trigger_fired={thread.trigger_fired_at is not None}"
                )

            out("  Latest private replies:")
            for ob in OutboundMessage.objects.filter(
                instagram_account=iba,
                action_type=OutboundMessage.ActionType.PRIVATE_REPLY,
            ).order_by("-pk")[:5]:
                out(
                    f"    #{ob.pk} {ob.status} attempts={ob.attempts} error={ob.last_error[:160]!r}"
                )
