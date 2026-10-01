"""Phase 2 reliability hardening: failure-path tests.

One test per hardening target so regressions surface precisely.
"""

from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import Account
from apps.whatsapp.models import (
    OutboundMessage,
    WhatsAppCampaign,
    WhatsAppCampaignRecipient,
    WhatsAppContact,
)
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.tasks import _notify_terminal_failure


def _make_account(slug):
    return Account.objects.create(company_name=slug, slug=slug)


class TokenExpiryTest(TestCase):
    """Error 190 from Meta marks the number's token as expired."""

    def setUp(self):
        self.account = _make_account("token-test")
        self.contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260971111111"
        )
        self.number = WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="PNID1",
            access_token="tok",
            waba_id="W1",
            registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
        )

    def _make_failed_msg(self, error_code):
        msg = OutboundMessage.objects.create(
            account=self.account,
            contact=self.contact,
            payload={"type": "text", "body": "hi"},
            status=OutboundMessage.Status.FAILED,
            error_code=error_code,
            last_error="Meta rejected the token",
        )
        return msg

    def test_error_190_sets_token_expired(self):
        msg = self._make_failed_msg("190")
        with patch(
            "apps.automation.integrations.whatsapp.mark_outbound_message_failed"
        ):
            _notify_terminal_failure(msg, phone_number_id=self.number.phone_number_id)
        self.number.refresh_from_db()
        self.assertTrue(self.number.token_expired)

    def test_error_190_does_not_flag_unrelated_number(self):
        # A second number on the same account must not be flagged when only the first fails.
        other = WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="PNID2",
            access_token="tok2",
            waba_id="W1",
            registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
        )
        msg = self._make_failed_msg("190")
        with patch(
            "apps.automation.integrations.whatsapp.mark_outbound_message_failed"
        ):
            _notify_terminal_failure(msg, phone_number_id=self.number.phone_number_id)
        other.refresh_from_db()
        self.assertFalse(other.token_expired)

    def test_other_error_does_not_set_token_expired(self):
        msg = self._make_failed_msg("131047")
        with patch(
            "apps.automation.integrations.whatsapp.mark_outbound_message_failed"
        ):
            _notify_terminal_failure(msg, phone_number_id=self.number.phone_number_id)
        self.number.refresh_from_db()
        self.assertFalse(self.number.token_expired)

    def test_setup_status_token_expired(self):
        self.number.token_expired = True
        self.number.save(update_fields=["token_expired"])
        self.assertEqual(
            self.number.setup_status,
            WhatsAppBusinessNumber.SetupStatus.TOKEN_EXPIRED,
        )

    def test_is_ready_false_when_token_expired(self):
        self.number.token_expired = True
        self.number.save(update_fields=["token_expired"])
        self.assertFalse(self.number.is_ready)

    def test_is_ready_true_when_token_not_expired(self):
        self.assertFalse(self.number.token_expired)
        # Without a test reply setup_status isn't READY, but is_ready checks
        # credentials only — token_expired is the new gate we're testing.
        # Verify the flag alone doesn't break the positive case.
        self.assertTrue(self.number.is_ready)


class CampaignFailurePropagationTest(TestCase):
    """Terminal OutboundMessage failure propagates to the campaign recipient row."""

    def setUp(self):
        self.account = _make_account("campaign-test")
        self.contact_obj = self.account.email_contacts.create(
            first_name="Test", phone="+260972222222"
        )
        self.wa_contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260972222222"
        )
        from apps.contacts.models import ContactList

        contact_list = ContactList.objects.create(account=self.account, name="List")
        from apps.whatsapp.models import MessageTemplate

        template = MessageTemplate.objects.create(
            account=self.account,
            name="Hello",
            whatsapp_template_name="hello",
            language_code="en",
            approval_status=MessageTemplate.ApprovalStatus.APPROVED,
            category=MessageTemplate.Category.UTILITY,
            content="Hello {{1}}",
        )
        self.campaign = WhatsAppCampaign.objects.create(
            account=self.account,
            name="Test Campaign",
            contact_list=contact_list,
            template=template,
            status=WhatsAppCampaign.Status.SENDING,
        )
        self.msg = OutboundMessage.objects.create(
            account=self.account,
            contact=self.wa_contact,
            payload={"type": "template", "template_name": "hello"},
            status=OutboundMessage.Status.FAILED,
            last_error="Template not approved",
            error_code="132001",
        )
        self.recipient = WhatsAppCampaignRecipient.objects.create(
            campaign=self.campaign,
            contact=self.contact_obj,
            message=self.msg,
            status=WhatsAppCampaignRecipient.Status.QUEUED,
        )

    def test_recipient_marked_failed_on_terminal_failure(self):
        with patch(
            "apps.automation.integrations.whatsapp.mark_outbound_message_failed"
        ):
            _notify_terminal_failure(self.msg)
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.status, WhatsAppCampaignRecipient.Status.FAILED)
        self.assertIn("Template not approved", self.recipient.error)

    def test_no_effect_when_message_not_terminal(self):
        self.msg.status = OutboundMessage.Status.QUEUED
        self.msg.save(update_fields=["status"])
        _notify_terminal_failure(self.msg)
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.status, WhatsAppCampaignRecipient.Status.QUEUED)
