"""Subscribe connected Pages to the current Messenger and comment webhook fields again.

    python manage.py facebook_resubscribe [--account <slug>] [--dry-run]

A Page is subscribed when it connects, with the fields of that day. When
``apps.facebook.services.pages.WEBHOOK_FIELDS`` gains a field (``messaging_referrals``,
14 November 2026: a thread reopened from a Click-to-Messenger ad), Pages connected earlier
keep the old list until this runs. Calls Meta once per active Page; prints which Pages
succeeded, never a token. Safe to run again.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Re-subscribe connected Facebook Pages to the current webhook fields"

    def add_arguments(self, parser):
        parser.add_argument("--account", help="Workspace slug (default: all)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from apps.facebook.services.pages import WEBHOOK_FIELDS, subscribe_page
        from apps.publishing.models import FacebookPageConnection

        conns = FacebookPageConnection.objects.filter(
            status=FacebookPageConnection.Status.ACTIVE
        ).select_related("account")
        if opts["account"]:
            conns = conns.filter(account__slug=opts["account"])
        self.stdout.write(f"fields: {WEBHOOK_FIELDS}")
        done = failed = 0
        for conn in conns:
            label = f"{conn.account.slug} page={conn.page_id}"
            if opts["dry_run"]:
                self.stdout.write(f"would subscribe {label}")
                continue
            if subscribe_page(conn):
                done += 1
                self.stdout.write(f"ok {label}")
            else:
                failed += 1
                self.stdout.write(f"FAILED {label}: {conn.webhook_error[:200]}")
        if not opts["dry_run"]:
            self.stdout.write(f"subscribed: {done}; failed: {failed}")
