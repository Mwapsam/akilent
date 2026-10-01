"""Management command: data integrity checks for production audit.

Checks for anomalies that FK constraints cannot prevent on their own
(cross-account mismatches, soft-duplicate keys, stuck processing rows).

Usage
-----
    poetry run python manage.py check_integrity
    poetry run python manage.py check_integrity --account <slug>
    poetry run python manage.py check_integrity --fix        # auto-fix safe issues

Exit codes
----------
    0 — all checks clean
    1 — one or more issues found (CI-safe: the command itself doesn't raise)
"""

import logging
import sys

from django.core.management.base import BaseCommand
from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)

_STUCK_CAMPAIGN_MINUTES = 60


class Command(BaseCommand):
    help = "Verify data integrity invariants across all accounts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            metavar="SLUG",
            help="Restrict checks to one account slug.",
        )
        parser.add_argument(
            "--fix",
            action="store_true",
            default=False,
            help="Auto-fix safe issues (stuck recipient rows only).",
        )

    def handle(self, *args, **options):
        from apps.accounts.models import Account

        slug = options.get("account")
        fix = options.get("fix", False)

        if slug:
            accounts = Account.objects.filter(slug=slug, is_active=True)
            if not accounts.exists():
                self.stderr.write(self.style.ERROR(f"No active account: {slug!r}"))
                sys.exit(1)
        else:
            accounts = Account.objects.filter(is_active=True)

        issues: list[str] = []

        for account in accounts.iterator():
            issues += self._check_contact_account_mismatch(account)
            issues += self._check_duplicate_phones(account)
            issues += self._check_duplicate_emails(account)
            issues += self._check_duplicate_idempotency_keys(account)
            issues += self._check_lead_account_mismatch(account)
            issues += self._check_stuck_campaign_recipients(account, fix=fix)

        if issues:
            for msg in issues:
                self.stderr.write(self.style.WARNING(f"  ISSUE: {msg}"))
            self.stderr.write(
                self.style.ERROR(f"\n{len(issues)} integrity issue(s) found.")
            )
            sys.exit(1)
        else:
            self.stdout.write(self.style.SUCCESS("All integrity checks passed."))

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_contact_account_mismatch(self, account) -> list[str]:
        """ContactPhone/Email rows that point to a contact owned by a different account."""
        from apps.contacts.models import ContactEmail, ContactPhone

        issues = []
        cross_phones = ContactPhone.objects.filter(contact__account=account).exclude(
            contact__account_id=account.pk
        )
        if cross_phones.exists():
            issues.append(
                f"[{account.slug}] {cross_phones.count()} ContactPhone row(s) "
                "belong to contacts from a different account"
            )

        cross_emails = ContactEmail.objects.filter(contact__account=account).exclude(
            contact__account_id=account.pk
        )
        if cross_emails.exists():
            issues.append(
                f"[{account.slug}] {cross_emails.count()} ContactEmail row(s) "
                "belong to contacts from a different account"
            )
        return issues

    def _check_duplicate_phones(self, account) -> list[str]:
        """Contacts sharing the same non-null phone within an account."""
        from apps.contacts.models import Contact

        dups = (
            Contact.objects.filter(account=account, phone__isnull=False)
            .values("phone")
            .annotate(n=models.Count("id"))
            .filter(n__gt=1)
        )
        if dups.exists():
            total = sum(d["n"] for d in dups)
            return [
                f"[{account.slug}] {dups.count()} duplicate phone value(s) "
                f"across {total} contact rows"
            ]
        return []

    def _check_duplicate_emails(self, account) -> list[str]:
        """Contacts sharing the same non-null email within an account."""
        from apps.contacts.models import Contact

        dups = (
            Contact.objects.filter(account=account, email__isnull=False)
            .values("email")
            .annotate(n=models.Count("id"))
            .filter(n__gt=1)
        )
        if dups.exists():
            total = sum(d["n"] for d in dups)
            return [
                f"[{account.slug}] {dups.count()} duplicate email value(s) "
                f"across {total} contact rows"
            ]
        return []

    def _check_duplicate_idempotency_keys(self, account) -> list[str]:
        """OutboundMessages with the same non-null idempotency_key in one account."""
        from apps.whatsapp.models import OutboundMessage

        dups = (
            OutboundMessage.objects.filter(
                account=account, idempotency_key__isnull=False
            )
            .exclude(idempotency_key="")
            .values("idempotency_key")
            .annotate(n=models.Count("id"))
            .filter(n__gt=1)
        )
        if dups.exists():
            return [
                f"[{account.slug}] {dups.count()} duplicate OutboundMessage "
                "idempotency key(s) — possible double-send"
            ]
        return []

    def _check_lead_account_mismatch(self, account) -> list[str]:
        """Leads whose contact belongs to a different account."""
        from apps.crm.models import Lead

        cross = Lead.objects.filter(account=account).exclude(contact__account=account)
        if cross.exists():
            return [
                f"[{account.slug}] {cross.count()} Lead(s) whose contact "
                "belongs to a different account"
            ]
        return []

    def _check_stuck_campaign_recipients(self, account, *, fix: bool) -> list[str]:
        """WhatsAppCampaignRecipient rows stuck PENDING on a completed campaign."""
        from apps.whatsapp.models import WhatsAppCampaign, WhatsAppCampaignRecipient

        cutoff = timezone.now() - timezone.timedelta(minutes=_STUCK_CAMPAIGN_MINUTES)
        stuck = WhatsAppCampaignRecipient.objects.filter(
            campaign__account=account,
            campaign__status=WhatsAppCampaign.Status.COMPLETED,
            campaign__completed_at__lt=cutoff,
            status=WhatsAppCampaignRecipient.Status.PENDING,
        )
        if not stuck.exists():
            return []

        count = stuck.count()
        if fix:
            stuck.update(
                status=WhatsAppCampaignRecipient.Status.FAILED,
                error="check_integrity: marked failed (was stuck PENDING on completed campaign)",
            )
            self.stdout.write(
                f"[{account.slug}] Fixed {count} stuck PENDING recipient(s)."
            )
            return []
        return [
            f"[{account.slug}] {count} WhatsAppCampaignRecipient row(s) stuck "
            f"PENDING on a completed campaign (run --fix to resolve)"
        ]
