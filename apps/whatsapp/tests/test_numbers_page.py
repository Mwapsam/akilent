"""WhatsApp Business onboarding page — rendering + state-driven messaging."""
import html
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from apps.accounts.models import Account
from apps.whatsapp import numbers as numbers_views
from apps.whatsapp.models import Conversation, MessageLog
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber


class OnboardingPageTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")
        self.user = User.objects.create_user("owner", "o@example.com", "pw")
        self.rf = RequestFactory()

    def _render(self, *, staff=False, module=True):
        request = self.rf.get("/whatsapp/numbers/")
        self.user.is_staff = staff
        request.user = self.user
        request.session = {}
        with patch(
            "apps.whatsapp.numbers.get_current_account", return_value=self.account
        ), patch("apps.billing.api.has_feature", return_value=module):
            resp = numbers_views.numbers_list(request)
        return resp.status_code, resp.content.decode()

    def _add_number(self, **kw):
        defaults = dict(
            account=self.account, phone_number_id="PNID", access_token="tok",
            is_active=True,
        )
        defaults.update(kw)
        return WhatsAppBusinessNumber.objects.create(**defaults)

    def test_empty_state(self):
        status, body = self._render()
        self.assertEqual(status, 200)
        self.assertIn("No number connected yet", body)
        self.assertIn("Setup progress", body)

    def test_plan_warning_when_module_disabled(self):
        _, body = self._render(module=False)
        self.assertIn("WhatsApp isn't enabled on this plan", body)

    def test_all_set_banner_when_fully_onboarded(self):
        self._add_number(
            waba_id="WABA1", verification_pin="123456", registration_status="registered"
        )
        contact = self.account.contacts.create(phone_number="+260971234567")
        convo = Conversation.get_or_open(contact)
        MessageLog.objects.create(
            account=self.account, conversation=convo, contact=contact,
            direction=MessageLog.Direction.INBOUND,
            message_type=MessageLog.MessageType.TEXT, content="hi",
            status=MessageLog.Status.DELIVERED, timestamp="2026-09-04T00:00:00Z",
        )
        number = WhatsAppBusinessNumber.objects.get(phone_number_id="PNID")
        number.connection_tests.create(recipient="+260971234567", status="sent")
        _, body = self._render()
        self.assertIn("You're all set", body)

    def test_unregistered_number_shows_warning(self):
        self._add_number(waba_id="WABA1", verification_pin=None)
        _, body = self._render()
        self.assertIn("isn't registered on the Cloud API yet", html.unescape(body))
        self.assertIn("Retry registration", body)

    def test_failed_registration_shows_error_and_retry(self):
        self._add_number(
            waba_id="WABA1", registration_status="failed", registration_error="133010 nope"
        )
        _, body = self._render()
        self.assertIn("133010 nope", body)
        self.assertIn("Retry registration", body)
        self.assertNotIn("re-run", body.lower())

    def test_missing_token_shows_warning(self):
        WhatsAppBusinessNumber.objects.create(
            account=self.account, phone_number_id="PNID", access_token=None,
            is_active=True,
        )
        _, body = self._render()
        self.assertIn("this number can't send", html.unescape(body))

    def test_webhook_block_is_staff_only(self):
        self._add_number()
        self.assertNotIn("Webhook (platform setup)", self._render(staff=False)[1])
        self.assertIn("Webhook (platform setup)", self._render(staff=True)[1])
