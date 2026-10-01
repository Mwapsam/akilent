"""Tests for the check_integrity management command (Phase 4).

Each test seeds a deliberately broken state, runs the command, and asserts it
reports the expected issue — then checks the --fix path where applicable.
"""

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase


def _make_account(slug, company="Test Corp"):
    from apps.accounts.models import Account, Membership

    user = User.objects.create_user(
        username=f"{slug}@x.com", email=f"{slug}@x.com", password="x"
    )
    account = Account.objects.create(
        company_name=company, slug=slug, selected_services="whatsapp"
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account, user


def _run(slug=None, fix=False):
    """Run check_integrity and return (stdout, stderr, exit_code)."""
    out, err = StringIO(), StringIO()
    try:
        args = ["check_integrity"]
        if slug:
            args += ["--account", slug]
        if fix:
            args += ["--fix"]
        call_command(*args, stdout=out, stderr=err)
        code = 0
    except SystemExit as exc:
        code = exc.code
    return out.getvalue(), err.getvalue(), code


class IntegrityCleanTest(TestCase):
    def test_clean_account_passes(self):
        _make_account("clean-acct")
        _, _, code = _run("clean-acct")
        self.assertEqual(code, 0)


def _drop_index(name):
    """Drop a named DB index for the duration of the current test.

    DDL is transactional in SQLite so the index is restored when the test
    transaction rolls back. This lets us seed otherwise-impossible duplicate
    rows to exercise the integrity checks.
    """
    from django.db import connection

    with connection.cursor() as c:
        c.execute(f"DROP INDEX IF EXISTS {name}")


class DuplicatePhoneTest(TestCase):
    def test_duplicate_phones_flagged(self):
        from apps.contacts.models import Contact

        account, _ = _make_account("dup-phone")
        _drop_index("unique_account_contact_phone")
        Contact.objects.create(account=account, phone="+260977000001", source="seed")
        Contact.objects.create(account=account, phone="+260977000001", source="seed")

        _, err, code = _run(account.slug)
        self.assertEqual(code, 1)
        self.assertIn("duplicate phone", err)


class DuplicateEmailTest(TestCase):
    def test_duplicate_emails_flagged(self):
        from apps.contacts.models import Contact

        account, _ = _make_account("dup-email")
        _drop_index("unique_account_contact_email")
        Contact.objects.create(account=account, email="same@example.com", source="seed")
        Contact.objects.create(account=account, email="same@example.com", source="seed")

        _, err, code = _run(account.slug)
        self.assertEqual(code, 1)
        self.assertIn("duplicate email", err)


class DuplicateIdempotencyKeyTest(TestCase):
    def test_duplicate_idempotency_keys_flagged(self):
        from apps.contacts.models import Contact
        from apps.whatsapp.models import OutboundMessage, WhatsAppContact

        account, _ = _make_account("dup-idem")
        contact = Contact.objects.create(account=account, phone="+260977111111")
        wa_contact = WhatsAppContact.objects.create(
            account=account,
            phone_number="+260977111111",
            contact=contact,
        )
        _drop_index("unique_outbound_idempotency")
        OutboundMessage.objects.create(
            account=account,
            contact=wa_contact,
            payload={"type": "template"},
            idempotency_key="dup-key-001",
        )
        OutboundMessage.objects.create(
            account=account,
            contact=wa_contact,
            payload={"type": "template"},
            idempotency_key="dup-key-001",
        )

        _, err, code = _run(account.slug)
        self.assertEqual(code, 1)
        self.assertIn("idempotency", err)


class LeadAccountMismatchTest(TestCase):
    def test_lead_with_wrong_contact_account_flagged(self):
        from apps.contacts.models import Contact
        from apps.crm.models import Lead

        account_a, _ = _make_account("lead-mismatch-a")
        account_b, _ = _make_account("lead-mismatch-b")

        contact_in_b = Contact.objects.create(account=account_b, phone="+260977222222")
        # Create a lead in account_a pointing to a contact in account_b.
        Lead.objects.create(account=account_a, contact=contact_in_b)

        _, err, code = _run(account_a.slug)
        self.assertEqual(code, 1)
        self.assertIn("different account", err)


class StuckCampaignRecipientsTest(TestCase):
    def test_stuck_pending_recipients_flagged_and_fixed(self):
        from django.utils import timezone

        from apps.contacts.models import Contact, ContactList
        from apps.whatsapp.models import (
            MessageTemplate,
            WhatsAppCampaign,
            WhatsAppCampaignRecipient,
        )

        account, user = _make_account("stuck-recip")
        contact = Contact.objects.create(account=account, phone="+260977333333")
        clist = ContactList.objects.create(account=account, name="L", slug="stuck-list")
        clist.contacts.set([contact])
        template = MessageTemplate.objects.create(
            account=account,
            name="tpl",
            whatsapp_template_name="tpl",
            approval_status=MessageTemplate.ApprovalStatus.APPROVED,
            category="MARKETING",
            language_code="en",
        )
        campaign = WhatsAppCampaign.objects.create(
            account=account,
            name="Stuck Camp",
            contact_list=clist,
            template=template,
            status=WhatsAppCampaign.Status.COMPLETED,
            completed_at=timezone.now() - timezone.timedelta(hours=2),
        )
        recipient = WhatsAppCampaignRecipient.objects.create(
            campaign=campaign,
            contact=contact,
            status=WhatsAppCampaignRecipient.Status.PENDING,
        )

        # Without --fix: should report the issue.
        _, err, code = _run(account.slug)
        self.assertEqual(code, 1)
        self.assertIn("stuck", err.lower())

        # With --fix: should resolve the row and return clean.
        _, _, fix_code = _run(account.slug, fix=True)
        self.assertEqual(fix_code, 0)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, WhatsAppCampaignRecipient.Status.FAILED)
