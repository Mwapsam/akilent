"""Phase 2 reliability hardening tests.

Each test proves one of the Phase 2 guarantees:

- Outbound idempotency: sending twice with the same key is a no-op.
- Inbound dedup: two MessageLog rows with the same wamid are rejected.
- Campaign opt-out enforcement: opted-out contacts are skipped before send.
- Campaign no-identity skip: contacts without a WhatsApp number are skipped.
- Campaign fan-out idempotency: re-running send_campaign never creates
  duplicate OutboundMessages.
- Delivery stats: campaign_delivery_stats returns correct structure.
- Lead dedup: create_lead for an existing open lead returns the same row.
- Status ordering: MessageLog.apply_status_update rejects backwards transitions.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone


def _make_account(slug):
    from apps.accounts.models import Account, Membership

    user = User.objects.create_user(
        username=f"{slug}@example.com",
        email=f"{slug}@example.com",
        password="x",
    )
    account = Account.objects.create(
        company_name="Reliability Corp",
        slug=slug,
        selected_services="whatsapp",
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account, user


def _make_wa_contact(account, phone="+260971000001", opted_out=False):
    from apps.contacts.models import Contact
    from apps.whatsapp.models import WhatsAppContact

    contact = Contact.objects.create(account=account, phone=phone, source="seed")
    wc = WhatsAppContact.objects.create(
        account=account,
        phone_number=phone,
        contact=contact,
    )
    if opted_out:
        wc.record_opt_out("user_replied_stop")
    return contact, wc


def _make_template(account):
    from apps.whatsapp.models import MessageTemplate

    return MessageTemplate.objects.create(
        account=account,
        name="reliability_tpl",
        whatsapp_template_name="reliability_tpl",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        category="MARKETING",
        language_code="en",
    )


def _make_campaign(account, user, contact_list, template):
    from apps.whatsapp.campaigns import create_and_queue_campaign

    with patch("apps.whatsapp.campaigns.send_campaign.delay"):
        return create_and_queue_campaign(
            account=account,
            name="Reliability Test",
            contact_list=contact_list,
            template_id=template.pk,
            created_by=user,
        )


class OutboundIdempotencyTest(TestCase):
    """send_message called twice with the same key returns the same row."""

    def setUp(self):
        self.account, _ = _make_account("idem-send")
        _, self.wc = _make_wa_contact(self.account)

    def test_same_key_returns_same_message(self):
        from apps.whatsapp.api import send_message

        key = "idempotency-test-key-001"
        with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
            m1 = send_message(self.account, self.wc, "Hello", idempotency_key=key)
            m2 = send_message(self.account, self.wc, "Hello again", idempotency_key=key)

        self.assertEqual(m1.pk, m2.pk)
        self.assertEqual(m1.payload["body"], "Hello")

    def test_different_keys_create_separate_messages(self):
        from apps.whatsapp.api import send_message

        with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
            m1 = send_message(self.account, self.wc, "A", idempotency_key="key-a")
            m2 = send_message(self.account, self.wc, "B", idempotency_key="key-b")

        self.assertNotEqual(m1.pk, m2.pk)

    def test_no_key_auto_generates_unique_key(self):

        from apps.whatsapp.api import send_message

        with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
            m1 = send_message(self.account, self.wc, "X")
            m2 = send_message(self.account, self.wc, "Y")

        self.assertNotEqual(m1.pk, m2.pk)
        self.assertIsNotNone(m1.idempotency_key)
        self.assertIsNotNone(m2.idempotency_key)
        self.assertNotEqual(m1.idempotency_key, m2.idempotency_key)


class InboundDuplicateDetectionTest(TestCase):
    """Two MessageLog rows with the same (account, message_id) are rejected."""

    def setUp(self):
        self.account, _ = _make_account("inbound-dedup")
        _, self.wc = _make_wa_contact(self.account)

    def _make_conversation(self):
        from apps.whatsapp.models import Conversation

        return Conversation.objects.create(
            account=self.account,
            contact=self.wc,
        )

    def test_duplicate_message_id_raises_integrity_error(self):
        from apps.whatsapp.models import MessageLog

        conv = self._make_conversation()
        MessageLog.objects.create(
            account=self.account,
            conversation=conv,
            contact=self.wc,
            direction=MessageLog.Direction.INBOUND,
            message_id="wamid.AAAAABBB001",
            timestamp=timezone.now(),
        )
        with self.assertRaises(IntegrityError):
            MessageLog.objects.create(
                account=self.account,
                conversation=conv,
                contact=self.wc,
                direction=MessageLog.Direction.INBOUND,
                message_id="wamid.AAAAABBB001",
                timestamp=timezone.now(),
            )

    def test_null_message_id_is_not_deduplicated(self):
        """Rows with message_id=None are allowed to coexist."""
        from apps.whatsapp.models import MessageLog

        conv = self._make_conversation()
        m1 = MessageLog.objects.create(
            account=self.account,
            conversation=conv,
            contact=self.wc,
            direction=MessageLog.Direction.INBOUND,
            message_id=None,
            timestamp=timezone.now(),
        )
        m2 = MessageLog.objects.create(
            account=self.account,
            conversation=conv,
            contact=self.wc,
            direction=MessageLog.Direction.INBOUND,
            message_id=None,
            timestamp=timezone.now(),
        )
        self.assertNotEqual(m1.pk, m2.pk)


class CampaignOptOutTest(TestCase):
    """Opted-out contacts are skipped before any OutboundMessage is created."""

    def setUp(self):
        from apps.contacts.models import ContactList

        self.account, self.user = _make_account("optout-camp")
        self.template = _make_template(self.account)

        self.opted_in_contact, self.opted_in_wc = _make_wa_contact(
            self.account, "+260971000010"
        )
        self.opted_out_contact, self.opted_out_wc = _make_wa_contact(
            self.account, "+260971000011", opted_out=True
        )

        self.clist = ContactList.objects.create(
            account=self.account, name="Mixed", slug="mixed-optout"
        )
        self.clist.contacts.set([self.opted_in_contact, self.opted_out_contact])

    def test_opted_out_contact_skipped_not_queued(self):
        from apps.whatsapp.campaigns import send_campaign
        from apps.whatsapp.models import WhatsAppCampaignRecipient

        campaign = _make_campaign(self.account, self.user, self.clist, self.template)
        with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
            send_campaign(campaign.pk)

        opted_out_recipient = WhatsAppCampaignRecipient.objects.get(
            campaign=campaign, contact=self.opted_out_contact
        )
        self.assertEqual(
            opted_out_recipient.status, WhatsAppCampaignRecipient.Status.SKIPPED
        )
        self.assertEqual(
            opted_out_recipient.skip_reason,
            WhatsAppCampaignRecipient.SkipReason.OPTED_OUT,
        )
        self.assertIsNone(opted_out_recipient.message)

    def test_opted_in_contact_queued(self):
        from apps.whatsapp.campaigns import send_campaign
        from apps.whatsapp.models import WhatsAppCampaignRecipient

        campaign = _make_campaign(self.account, self.user, self.clist, self.template)
        with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
            send_campaign(campaign.pk)

        opted_in_recipient = WhatsAppCampaignRecipient.objects.get(
            campaign=campaign, contact=self.opted_in_contact
        )
        self.assertEqual(
            opted_in_recipient.status, WhatsAppCampaignRecipient.Status.QUEUED
        )


class CampaignNoIdentityTest(TestCase):
    """Contacts without a linked WhatsApp number are skipped."""

    def setUp(self):
        from apps.contacts.models import Contact, ContactList

        self.account, self.user = _make_account("no-identity-camp")
        self.template = _make_template(self.account)

        # A contact with no WhatsAppContact row
        self.bare_contact = Contact.objects.create(
            account=self.account, phone="+260971000020", source="seed"
        )

        self.clist = ContactList.objects.create(
            account=self.account, name="Bare", slug="bare-list"
        )
        self.clist.contacts.set([self.bare_contact])

    def test_contact_without_whatsapp_identity_skipped(self):
        from apps.whatsapp.campaigns import send_campaign
        from apps.whatsapp.models import WhatsAppCampaignRecipient

        campaign = _make_campaign(self.account, self.user, self.clist, self.template)
        send_campaign(campaign.pk)

        recipient = WhatsAppCampaignRecipient.objects.get(
            campaign=campaign, contact=self.bare_contact
        )
        self.assertEqual(recipient.status, WhatsAppCampaignRecipient.Status.SKIPPED)
        self.assertEqual(
            recipient.skip_reason,
            WhatsAppCampaignRecipient.SkipReason.NO_WHATSAPP_IDENTITY,
        )


class CampaignFanOutIdempotencyTest(TestCase):
    """Re-running send_campaign never creates duplicate OutboundMessages."""

    def setUp(self):
        from apps.contacts.models import ContactList

        self.account, self.user = _make_account("fanout-idem")
        self.template = _make_template(self.account)

        self.contact, self.wc = _make_wa_contact(self.account, "+260971000030")
        self.clist = ContactList.objects.create(
            account=self.account, name="Idem", slug="idem-fanout"
        )
        self.clist.contacts.set([self.contact])

    def test_second_run_skipped_when_campaign_completed(self):
        """A completed campaign returns early — status gate is the first defence."""
        from apps.whatsapp.campaigns import send_campaign
        from apps.whatsapp.models import OutboundMessage

        campaign = _make_campaign(self.account, self.user, self.clist, self.template)
        with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
            send_campaign(campaign.pk)

        count_after_first = OutboundMessage.objects.filter(account=self.account).count()

        # Reset recipients to PENDING but leave campaign COMPLETED.
        from apps.whatsapp.models import WhatsAppCampaignRecipient

        WhatsAppCampaignRecipient.objects.filter(campaign=campaign).update(
            status=WhatsAppCampaignRecipient.Status.PENDING
        )
        # Second run returns early because campaign is COMPLETED — no new rows.
        send_campaign(campaign.pk)

        count_after_second = OutboundMessage.objects.filter(
            account=self.account
        ).count()
        self.assertEqual(count_after_first, count_after_second)

    def test_idempotency_key_constraint_is_db_backstop(self):
        """If the same recipient row is somehow presented twice, the DB
        unique constraint on idempotency_key prevents a duplicate OutboundMessage."""
        from django.db import IntegrityError

        from apps.whatsapp.campaigns import send_campaign
        from apps.whatsapp.models import (
            WhatsAppCampaign,
            WhatsAppCampaignRecipient,
        )

        campaign = _make_campaign(self.account, self.user, self.clist, self.template)
        with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
            send_campaign(campaign.pk)

        # Reset BOTH campaign status AND recipients → the task will attempt to
        # bulk_create OutboundMessages whose idempotency_key already exists.
        WhatsAppCampaignRecipient.objects.filter(campaign=campaign).update(
            status=WhatsAppCampaignRecipient.Status.PENDING
        )
        WhatsAppCampaign.objects.filter(pk=campaign.pk).update(
            status=WhatsAppCampaign.Status.QUEUED
        )
        campaign.refresh_from_db()

        with self.assertRaises(IntegrityError):
            send_campaign(campaign.pk)


class CampaignDeliveryStatsTest(TestCase):
    """campaign_delivery_stats returns the correct counts structure."""

    def setUp(self):
        from apps.contacts.models import ContactList

        self.account, self.user = _make_account("stats-camp")
        self.template = _make_template(self.account)
        self.contact, self.wc = _make_wa_contact(self.account, "+260971000040")
        self.clist = ContactList.objects.create(
            account=self.account, name="Stats", slug="stats-list"
        )
        self.clist.contacts.set([self.contact])

    def test_stats_has_expected_keys(self):
        from apps.whatsapp.campaigns import campaign_delivery_stats

        campaign = _make_campaign(self.account, self.user, self.clist, self.template)
        stats = campaign_delivery_stats(campaign)

        self.assertIn("sent", stats)
        self.assertIn("delivered", stats)
        self.assertIn("read", stats)
        self.assertIn("failed", stats)
        self.assertIn("pending", stats)
        self.assertTrue(all(isinstance(v, int) for v in stats.values()))


class LeadDedupTest(TestCase):
    """create_lead for a contact with an existing open lead returns the same row."""

    def setUp(self):
        from apps.contacts.models import Contact

        self.account, _ = _make_account("lead-dedup")
        self.contact = Contact.objects.create(
            account=self.account, phone="+260971000050", source="seed"
        )

    def test_second_create_returns_existing_lead(self):
        from apps.crm.services import create_lead

        lead1 = create_lead(self.account, self.contact, source="test")
        lead2 = create_lead(self.account, self.contact, source="test")

        self.assertEqual(lead1.pk, lead2.pk)

    def test_lead_count_stays_at_one(self):
        from apps.crm.models import Lead
        from apps.crm.services import create_lead

        create_lead(self.account, self.contact)
        create_lead(self.account, self.contact)
        create_lead(self.account, self.contact)

        self.assertEqual(
            Lead.objects.filter(account=self.account, contact=self.contact).count(), 1
        )


class MessageStatusOrderingTest(TestCase):
    """MessageLog.apply_status_update never moves status backwards."""

    def setUp(self):
        self.account, _ = _make_account("msg-order")
        _, self.wc = _make_wa_contact(self.account, "+260971000060")

    def _make_message_log(self, status):
        from apps.whatsapp.models import Conversation, MessageLog

        conv = Conversation.objects.create(account=self.account, contact=self.wc)
        return MessageLog.objects.create(
            account=self.account,
            conversation=conv,
            contact=self.wc,
            direction=MessageLog.Direction.OUTBOUND,
            timestamp=timezone.now(),
            status=status,
        )

    def test_delivered_does_not_revert_to_sent(self):
        from apps.whatsapp.models import MessageLog

        log = self._make_message_log(MessageLog.Status.DELIVERED)
        updated = log.apply_status_update(MessageLog.Status.SENT)

        self.assertFalse(updated)
        log.refresh_from_db()
        self.assertEqual(log.status, MessageLog.Status.DELIVERED)

    def test_read_does_not_revert_to_delivered(self):
        from apps.whatsapp.models import MessageLog

        log = self._make_message_log(MessageLog.Status.READ)
        updated = log.apply_status_update(MessageLog.Status.DELIVERED)

        self.assertFalse(updated)
        log.refresh_from_db()
        self.assertEqual(log.status, MessageLog.Status.READ)

    def test_queued_advances_to_delivered(self):
        from apps.whatsapp.models import MessageLog

        log = self._make_message_log(MessageLog.Status.QUEUED)
        updated = log.apply_status_update(MessageLog.Status.DELIVERED)

        self.assertTrue(updated)
        log.refresh_from_db()
        self.assertEqual(log.status, MessageLog.Status.DELIVERED)
