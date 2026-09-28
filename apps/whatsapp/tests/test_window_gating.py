"""Phase 2: send-time policy gating — 24h window + template approval.

``_authorize_send`` must:

* block free-text / media when the 24h customer-service window is closed
  (terminal failure, code OUTSIDE_WINDOW_NO_TEMPLATE);
* allow free-text while the window is open;
* allow a template send only when a Meta-approved MessageTemplate is linked;
* reject a template send with no template or an unapproved one
  (terminal failure, code TEMPLATE_NOT_APPROVED).

Terminal failures must not consume the retry budget.
"""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.whatsapp import api as whatsapp_api
from apps.whatsapp.models import (
    Conversation,
    MessageTemplate,
    OutboundMessage,
)
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.tasks import drain_outbound_queue
from apps.whatsapp.types import SendResult


class _FakeProvider:
    def __init__(self):
        self.calls = []

    def send_text(self, to, body):
        self.calls.append(("text", to, body))
        return SendResult(message_id="wamid.OK", success=True)

    def send_template(self, to, name, language, components):
        self.calls.append(("template", to, name))
        return SendResult(message_id="wamid.OK", success=True)


class SendGatingTest(TestCase):
    def setUp(self):
        patcher = patch("apps.whatsapp.tasks.drain_outbound_queue.delay")
        self.addCleanup(patcher.stop)
        patcher.start()

        self.account = Account.objects.create(company_name="Test Co", slug="test-co")
        WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="123456789",
            access_token="tok",
            is_active=True,
        )
        self.contact = whatsapp_api.get_or_create_contact(
            self.account, "+260971234567", "Test User"
        )
        self.provider = _FakeProvider()

    def _drain(self):
        with patch(
            "apps.whatsapp.tasks._get_provider_for_account", return_value=self.provider
        ):
            drain_outbound_queue()

    def _open_window(self):
        convo = Conversation.get_or_open(self.contact)
        convo.register_inbound(timezone.now())

    # --- free-text window gating -------------------------------------

    def test_free_text_blocked_when_window_closed(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        self._drain()

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.FAILED)
        self.assertIn("OUTSIDE_WINDOW_NO_TEMPLATE", msg.last_error)
        self.assertEqual(msg.attempts, 1)  # terminal — no retry scheduled
        self.assertIsNone(msg.next_attempt_at)
        self.assertEqual(self.provider.calls, [])

    def test_free_text_allowed_when_window_open(self):
        self._open_window()
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        self._drain()

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.SENT)
        self.assertEqual(len(self.provider.calls), 1)

    # --- template gating ------------------------------------------

    def _queue_template(self, template):
        return OutboundMessage.objects.create(
            account=self.account,
            contact=self.contact,
            template=template,
            idempotency_key="tmpl-1",
            payload={
                "type": "template",
                "template_name": template.whatsapp_template_name if template else "x",
                "language": "en",
                "components": [],
            },
        )

    def test_approved_template_allowed_outside_window(self):
        template = MessageTemplate.objects.create(
            account=self.account,
            name="Order update",
            whatsapp_template_name="order_update",
            approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        )
        msg = self._queue_template(template)
        self._drain()

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.SENT)
        self.assertEqual(
            self.provider.calls, [("template", "+260971234567", "order_update")]
        )

    def test_unapproved_template_rejected(self):
        template = MessageTemplate.objects.create(
            account=self.account,
            name="Promo",
            whatsapp_template_name="promo",
            approval_status=MessageTemplate.ApprovalStatus.PENDING,
        )
        msg = self._queue_template(template)
        self._drain()

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.FAILED)
        self.assertIn("TEMPLATE_NOT_APPROVED", msg.last_error)
        self.assertEqual(self.provider.calls, [])

    def test_template_type_without_linked_template_rejected(self):
        msg = self._queue_template(None)
        self._drain()

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.FAILED)
        self.assertIn("TEMPLATE_NOT_APPROVED", msg.last_error)
        self.assertEqual(self.provider.calls, [])
