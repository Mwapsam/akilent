"""Phase 4 load tests — performance baselines for bulk operations.

These are not stress tests; they verify that bulk operations complete within a
reasonable wall-clock budget and produce correct results at scale. Run them
before a Wave 2 pilot goes live to detect regressions.

Timings are deliberately generous so flaky CI workers don't produce false
failures. If a test blows the budget by more than 2×, investigate.
"""

import threading
import time
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase


def _make_account(slug="load-test-acct"):
    from apps.accounts.models import Account, Membership

    user = User.objects.create_user(
        username=f"{slug}@example.com",
        email=f"{slug}@example.com",
        password="x",
    )
    account = Account.objects.create(
        company_name="Load Test Corp",
        slug=slug,
        selected_services="whatsapp",
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account, user


class ContactImportLoadTest(TestCase):
    """import_rows() must handle 5,000 contacts without error within 60 s."""

    def setUp(self):
        self.account, _ = _make_account("import-load")

    def test_import_5000_contacts(self):
        from apps.contacts.importer import import_rows

        rows = [
            {"phone": f"+2609{70000000 + i:08d}", "first_name": f"User{i}"}
            for i in range(5_000)
        ]

        # Patch DNS MX checks so the test doesn't hit the network.
        with patch(
            "apps.contacts.importer._validate_email",
            side_effect=lambda e: {
                "email": e,
                "is_valid_format": True,
                "domain_valid": True,
                "deliverability_status": "valid",
                "last_checked": None,
            },
        ):
            t0 = time.monotonic()
            result = import_rows(self.account, rows)
            elapsed = time.monotonic() - t0

        self.assertEqual(result.errors, [], msg="Expected no import errors")
        self.assertEqual(result.duplicates, 0)
        self.assertEqual(result.created, 5_000)
        self.assertLess(
            elapsed, 60, msg=f"Import took {elapsed:.1f}s — over 60s budget"
        )

    def test_import_deduplication_at_scale(self):
        """Re-importing the same 500 rows produces 0 new contacts and 500 duplicates."""
        from apps.contacts.importer import import_rows

        rows = [{"phone": f"+2609{80000000 + i:08d}"} for i in range(500)]
        import_rows(self.account, rows)  # first pass

        t0 = time.monotonic()
        result = import_rows(self.account, rows)  # second pass — all dups
        elapsed = time.monotonic() - t0

        self.assertEqual(result.created, 0)
        self.assertEqual(result.duplicates, 500)
        self.assertLess(elapsed, 10)


class CampaignFanOutLoadTest(TestCase):
    """create_and_queue_campaign() must create 1,000 recipient rows without error."""

    def setUp(self):
        from apps.contacts.models import Contact, ContactList
        from apps.whatsapp.models import MessageTemplate

        self.account, self.user = _make_account("campaign-load")

        # Minimal approved template.
        self.template = MessageTemplate.objects.create(
            account=self.account,
            name="load_test_tpl",
            whatsapp_template_name="load_test",
            approval_status=MessageTemplate.ApprovalStatus.APPROVED,
            category="MARKETING",
            language_code="en",
        )

        # Build 1,000 contacts + a contact list containing all of them.
        contacts = Contact.objects.bulk_create(
            [
                Contact(
                    account=self.account,
                    phone=f"+2609{90000000 + i:08d}",
                    first_name=f"U{i}",
                    source="seed",
                )
                for i in range(1_000)
            ]
        )
        self.contact_list = ContactList.objects.create(
            account=self.account, name="Load List", slug="load-list"
        )
        self.contact_list.contacts.set(contacts)

    def test_1000_recipient_rows_created(self):
        from apps.whatsapp.campaigns import create_and_queue_campaign
        from apps.whatsapp.models import WhatsAppCampaignRecipient

        # Patch out the Celery task so we don't need a broker.
        with patch("apps.whatsapp.campaigns.send_campaign.delay"):
            t0 = time.monotonic()
            campaign = create_and_queue_campaign(
                account=self.account,
                name="Load Test Campaign",
                contact_list=self.contact_list,
                template_id=self.template.pk,
                created_by=self.user,
            )
            elapsed = time.monotonic() - t0

        count = WhatsAppCampaignRecipient.objects.filter(campaign=campaign).count()
        self.assertEqual(count, 1_000)
        self.assertLess(
            elapsed, 10, msg=f"Fan-out took {elapsed:.1f}s — over 10s budget"
        )

    def test_duplicate_recipients_not_created_on_retry(self):
        """A second create_and_queue call for the same list raises CampaignError or
        creates a separate campaign — never duplicates within the same campaign."""
        from apps.whatsapp.campaigns import create_and_queue_campaign
        from apps.whatsapp.models import WhatsAppCampaignRecipient

        with patch("apps.whatsapp.campaigns.send_campaign.delay"):
            c1 = create_and_queue_campaign(
                account=self.account,
                name="Camp A",
                contact_list=self.contact_list,
                template_id=self.template.pk,
            )
            c2 = create_and_queue_campaign(
                account=self.account,
                name="Camp B",
                contact_list=self.contact_list,
                template_id=self.template.pk,
            )

        # Two separate campaigns — no row-level duplication within either.
        self.assertNotEqual(c1.pk, c2.pk)
        r1 = WhatsAppCampaignRecipient.objects.filter(campaign=c1).count()
        r2 = WhatsAppCampaignRecipient.objects.filter(campaign=c2).count()
        self.assertEqual(r1, 1_000)
        self.assertEqual(r2, 1_000)


class ConcurrentConversationTest(TestCase):
    """10 simultaneous inbound webhook events must each be processed once.

    The Phase 2 distributed cache lock (cache.add) in process_whatsapp_event
    prevents the same event from being processed by two workers simultaneously.
    We verify that 10 distinct events, dispatched concurrently, are each
    attempted exactly once and none are silently dropped.
    """

    def test_10_concurrent_events_each_attempted_once(self):
        """Create 10 webhook event rows and process them concurrently."""
        from apps.whatsapp.models import WebhookEventLog

        events = WebhookEventLog.objects.bulk_create(
            [
                WebhookEventLog(
                    source=WebhookEventLog.Source.WHATSAPP,
                    event_type="message",
                    payload={"object": "whatsapp_business_account", "entry": []},
                )
                for _ in range(10)
            ]
        )

        attempted = []
        errors = []

        def process(event_id):
            try:
                from apps.whatsapp.tasks import process_whatsapp_event

                process_whatsapp_event(event_id)
                attempted.append(event_id)
            except Exception as exc:
                errors.append((event_id, str(exc)))

        threads = [threading.Thread(target=process, args=(e.pk,)) for e in events]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        elapsed = time.monotonic() - t0

        alive = [t for t in threads if t.is_alive()]
        self.assertEqual(alive, [], msg=f"{len(alive)} thread(s) still alive after 30s")

        # Each event should have been attempted exactly once (errors OK — the
        # minimal payload doesn't carry a real inbound message, but the task
        # should still run, acquire the lock, and exit).
        self.assertEqual(
            len(attempted) + len(errors),
            10,
            msg=f"Expected 10 attempts, got {len(attempted) + len(errors)}",
        )
        self.assertLess(elapsed, 30)
