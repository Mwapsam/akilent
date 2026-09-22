"""Per-number health panel and the DEGRADED derived state."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.whatsapp import numbers as numbers_views
from apps.whatsapp.health import DEGRADED_STREAK, is_degraded, number_health
from apps.whatsapp.models import Conversation, MessageLog, OutboundMessage, WebhookEventLog
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber as N

R = N.RegistrationStatus
S = N.SetupStatus
TESTER = "+260971234567"


class HealthBase(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")
        self.contact = self.account.contacts.create(phone_number=TESTER)
        self.number = N.objects.create(
            account=self.account, phone_number_id="PNID", access_token="tok",
            waba_id="W", registration_status=R.REGISTERED,
        )

    def outbound(self, status, *, ago=timedelta(minutes=1), code="", err=""):
        m = OutboundMessage.objects.create(
            account=self.account, contact=self.contact, payload={},
            status=status, error_code=code, last_error=err,
        )
        stamp = timezone.now() - ago
        OutboundMessage.objects.filter(pk=m.pk).update(updated_at=stamp, sent_at=stamp)
        return m

    def make_ready(self):
        self.number.connection_tests.create(recipient=TESTER, status="sent")
        convo = Conversation.get_or_open(self.contact)
        MessageLog.objects.create(
            account=self.account, conversation=convo, contact=self.contact,
            direction=MessageLog.Direction.INBOUND, message_type="text", content="hi",
            status="delivered", timestamp=timezone.now() + timedelta(seconds=1),
        )


class DegradedTest(HealthBase):
    def test_ready_when_no_failures(self):
        self.make_ready()
        self.assertEqual(self.number.setup_status, S.READY)

    def test_single_failure_stays_ready(self):
        self.make_ready()
        self.outbound("failed")
        self.assertEqual(self.number.setup_status, S.READY)

    def test_repeated_terminal_failures_degrade(self):
        self.make_ready()
        for _ in range(DEGRADED_STREAK):
            self.outbound("failed", code="190", err="token expired")
        self.assertEqual(self.number.setup_status, S.DEGRADED)

    def test_successful_send_restores_ready(self):
        self.make_ready()
        for i in range(DEGRADED_STREAK):
            self.outbound("failed", ago=timedelta(minutes=10 + i))
        self.assertEqual(self.number.setup_status, S.DEGRADED)
        self.outbound("sent", ago=timedelta(seconds=5))
        self.assertEqual(self.number.setup_status, S.READY)

    def test_old_failures_do_not_degrade(self):
        for _ in range(DEGRADED_STREAK):
            self.outbound("failed", ago=timedelta(days=3))
        self.assertFalse(is_degraded(self.account))

    def test_queued_and_retrying_messages_are_ignored(self):
        for _ in range(DEGRADED_STREAK):
            self.outbound("queued")
        self.assertFalse(is_degraded(self.account))


class NumberHealthTest(HealthBase):
    def _h(self, **kw):
        return number_health(self.number, **kw)

    def _items(self, h, title):
        return next(g for g in h["groups"] if g["title"] == title)["items"]

    def test_failed_registration_shows_meta_error_and_retry(self):
        self.number.registration_status = R.FAILED
        self.number.registration_error = "133010 nope"
        self.number.save()
        h = self._h()
        reg = self._items(h, "Connection")[2]
        self.assertEqual(reg["state"], "warn")
        self.assertIn("133010 nope", reg["detail"])
        self.assertEqual(h["headline"], "Attention needed")
        retry = h["actions"][0]
        self.assertEqual((retry["label"], retry["method"]), ("Retry registration", "post"))

    def test_missing_token_offers_reconnect(self):
        self.number.access_token = None
        self.number.save()
        h = self._h(embedded_enabled=True)
        self.assertEqual(self._items(h, "Connection")[1]["state"], "warn")
        self.assertIn("Reconnect", [a["label"] for a in h["actions"]])

    def test_untested_registered_number_is_ready_to_test(self):
        h = self._h()
        self.assertEqual(h["headline"], "Ready to test")
        self.assertEqual(self._items(h, "Messaging")[0]["state"], "none")
        self.assertEqual(self._items(h, "Webhooks")[0]["state"], "none")

    def test_ready_number_shows_open_inbox(self):
        self.make_ready()
        h = self._h()
        self.assertEqual(h["headline"], "Active")
        self.assertIn("Open Inbox", [a["label"] for a in h["actions"]])

    def test_degraded_lists_failure_reason(self):
        self.make_ready()
        for _ in range(DEGRADED_STREAK):
            self.outbound("failed", code="190", err="token expired")
        h = self._h()
        self.assertEqual(h["headline"], "Attention needed")
        failing = [i for i in self._items(h, "Messaging") if i["label"] == "Recent sends failing"]
        # Translated via apps.whatsapp.friendly_errors — no raw code/exception text.
        self.assertIn("reconnected", failing[0]["detail"])

    def test_last_webhook_is_matched_by_phone_number_id(self):
        def event(pnid):
            return WebhookEventLog.objects.create(
                source="whatsapp", event_type="message",
                payload={"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid}}}]}]},
            )
        event("OTHER")
        self.assertEqual(self._items(self._h(), "Webhooks")[0]["state"], "none")
        event("PNID")
        item = self._items(self._h(), "Webhooks")[0]
        self.assertEqual(item["state"], "ok")
        self.assertIsNotNone(item["when"])


class HealthPanelRenderTest(HealthBase):
    def test_page_renders_panel_and_no_legacy_badges(self):
        request = RequestFactory().get("/whatsapp/numbers/")
        request.user = User.objects.create_user("u", password="p")
        request.session = {}
        request._messages = FallbackStorage(request)
        with patch("apps.whatsapp.numbers.get_current_account", return_value=self.account), patch(
            "apps.billing.api.has_feature", return_value=True
        ), patch("apps.whatsapp.embedded.fetch_display_number", return_value=""):
            body = numbers_views.numbers_list(request).content.decode()
        for text in ("Connection", "Messaging", "Webhooks", "Ready to test", "Troubleshoot"):
            self.assertIn(text, body)
        self.assertNotIn("Cloud API registered", body)
