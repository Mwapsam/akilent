"""Store Meta ad referrals from messages that arrived before capture existed.

    python manage.py ad_referrals_backfill [--account <slug>] [--days 365] [--dry-run]

Reads each inbound WhatsApp message's stored webhook payload and each Instagram and
Messenger message's stored webhook entry, and records the ad it came from (apps.conversations.ad_referrals).
Idempotent: a second run changes nothing. Leads and orders already created keep the
attribution they were given (attributions are immutable); only new ones see the ad.
Instagram's and Messenger's standalone "reopened from an ad" events were never stored, so
they can't be recovered.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone


class Command(BaseCommand):
    help = "Backfill Meta ad referrals from stored WhatsApp, Instagram and Messenger webhooks"

    def add_arguments(self, parser):
        parser.add_argument("--account", help="Workspace slug (default: all)")
        parser.add_argument("--days", type=int, default=365)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from apps.conversations import ad_referrals
        from apps.conversations.models import AdReferral, Message

        messages = Message.objects.filter(
            Q(whatsapp_message__isnull=False)
            | Q(instagram_message__isnull=False)
            | Q(facebook_message__isnull=False),
            direction=Message.Direction.INBOUND,
            timestamp__gte=timezone.now() - timedelta(days=opts["days"]),
        ).select_related(
            "conversation", "whatsapp_message", "instagram_message", "facebook_message"
        )
        if opts["account"]:
            messages = messages.filter(account__slug=opts["account"])

        found = stored = 0
        before = AdReferral.objects.count()
        for message in messages.iterator(chunk_size=500):
            channel, data = ad_referrals.referral_for_message(message)
            if not data:
                continue
            found += 1
            if not opts["dry_run"]:
                ad_referrals.record(message.conversation, message, channel, data)
        if not opts["dry_run"]:
            stored = AdReferral.objects.count() - before
        verb = "would store" if opts["dry_run"] else "stored"
        self.stdout.write(
            f"messages with an ad referral: {found}; {verb}: "
            f"{found if opts['dry_run'] else stored} new"
        )
