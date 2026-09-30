"""Phase 3: messaging consent — inbound STOP/START keywords + send enforcement.

* An inbound "STOP" opts the contact out, closes the conversation, and queues a
  one-off confirmation reply that is itself allowed past the opt-out block.
* A normal send to an opted-out contact is refused (terminal, CONTACT_OPTED_OUT).
* An inbound "START" opts the contact back in.
* Marketing templates require an explicit opt-in even when approved.
"""

import hashlib
import hmac
import json
from io import BytesIO
from unittest.mock import patch

from django.http import HttpRequest
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Account
from apps.whatsapp import api as whatsapp_api
from apps.whatsapp.models import (
    Conversation,
    MessageTemplate,
    OutboundMessage,
    WhatsAppContact,
)
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.tasks import drain_outbound_queue, process_whatsapp_event
from apps.whatsapp.types import SendResult
from apps.whatsapp.views import WhatsAppWebhookView


def _signed_post(payload: dict, secret: str = "test_app_secret") -> HttpRequest:
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = HttpRequest()
    request.method = "POST"
    request._stream = BytesIO(body)
    request._body = body
    request.META = {
        "CONTENT_TYPE": "application/json",
        "HTTP_X_HUB_SIGNATURE_256": f"sha256={sig}",
        "REMOTE_ADDR": "1.2.3.4",
    }
    return request


def _inbound_text(body: str, wa_id="+260971234567", msg_id="wamid.IN1") -> dict:
    ts = int(timezone.now().timestamp())
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "123456789"},
                            "messages": [
                                {
                                    "id": msg_id,
                                    "from": wa_id,
                                    "timestamp": str(ts),
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                            "contacts": [{"profile": {"name": "Test User"}}],
                        }
                    }
                ]
            }
        ]
    }


class _FakeProvider:
    def __init__(self):
        self.calls = []

    def send_text(self, to, body):
        self.calls.append(("text", to, body))
        return SendResult(message_id="wamid.OUT", success=True)

    def send_template(self, to, name, language, components, category=""):
        self.calls.append(("template", to, name))
        return SendResult(message_id="wamid.OUT", success=True)


@override_settings(
    WHATSAPP_VERIFY_TOKEN="test_verify_token",
    WHATSAPP_APP_SECRET="test_app_secret",
    CELERY_TASK_ALWAYS_EAGER=True,
)
class ConsentKeywordTest(TestCase):
    def setUp(self):
        from django.core.cache import cache

        from apps.billing.models import Plan, Subscription

        # _automation_events_enabled() caches its flag for 60s in LocMemCache,
        # which outlives a test; keep this class hermetic.
        cache.clear()
        self.addCleanup(cache.clear)

        self.account = Account.objects.create(company_name="Test Co", slug="test-co")
        WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="123456789",
            access_token="tok",
            is_active=True,
        )
        plan = Plan.objects.first() or Plan.objects.create(
            name="Test Plan",
            slug="test-plan",
            price_monthly=0,
            max_conversations_per_month=-1,
        )
        Subscription.objects.create(
            account=self.account,
            plan=plan,
            status=Subscription.ACTIVE,
            current_period_start=timezone.now(),
        )
        self.provider = _FakeProvider()
        p = patch(
            "apps.whatsapp.tasks._get_provider_for_account", return_value=self.provider
        )
        self.addCleanup(p.stop)
        p.start()

    def _ingest(self, payload):
        view = WhatsAppWebhookView()
        resp = view.post(_signed_post(payload))
        self.assertEqual(resp.status_code, 200)
        from apps.whatsapp.models import WebhookEventLog

        for ev in WebhookEventLog.objects.filter(processed=False):
            process_whatsapp_event(ev.id)

    def test_stop_keyword_opts_out_closes_convo_and_confirms(self):
        self._ingest(_inbound_text("Hello", msg_id="wamid.A"))
        contact = WhatsAppContact.objects.get(account=self.account)
        self.assertEqual(contact.opt_in_status, WhatsAppContact.OptInStatus.UNKNOWN)

        self._ingest(_inbound_text("STOP", msg_id="wamid.B"))
        contact.refresh_from_db()
        self.assertEqual(contact.opt_in_status, WhatsAppContact.OptInStatus.OPTED_OUT)
        self.assertIsNotNone(contact.opt_out_at)
        self.assertFalse(
            Conversation.objects.filter(contact=contact, is_open=True).exists()
        )
        # The confirmation reply was queued and delivered despite the opt-out.
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.provider.calls[0][0], "text")
        ack = OutboundMessage.objects.get(contact=contact)
        self.assertTrue(ack.payload.get("_consent_ack"))
        self.assertEqual(ack.status, OutboundMessage.Status.SENT)

    def test_send_to_opted_out_contact_is_refused(self):
        self._ingest(_inbound_text("STOP", msg_id="wamid.C"))
        contact = WhatsAppContact.objects.get(account=self.account)
        self.provider.calls.clear()

        with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
            msg = whatsapp_api.send_message(self.account, contact, "promo!")
        drain_outbound_queue()

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.FAILED)
        self.assertIn("CONTACT_OPTED_OUT", msg.last_error)
        self.assertEqual(self.provider.calls, [])

    def test_start_keyword_opts_back_in(self):
        self._ingest(_inbound_text("STOP", msg_id="wamid.D"))
        self._ingest(_inbound_text("START", msg_id="wamid.E"))
        contact = WhatsAppContact.objects.get(account=self.account)
        self.assertEqual(contact.opt_in_status, WhatsAppContact.OptInStatus.OPTED_IN)
        self.assertIsNotNone(contact.opt_in_at)

    def test_marketing_template_requires_opt_in(self):
        contact = whatsapp_api.get_or_create_contact(
            self.account, "+260971234567", "Test User"
        )
        template = MessageTemplate.objects.create(
            account=self.account,
            name="Promo",
            whatsapp_template_name="promo",
            category=MessageTemplate.Category.MARKETING,
            approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        )
        msg = OutboundMessage.objects.create(
            account=self.account,
            contact=contact,
            template=template,
            idempotency_key="m1",
            payload={
                "type": "template",
                "template_name": "promo",
                "language": "en",
                "components": [],
            },
        )
        drain_outbound_queue()
        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.FAILED)
        self.assertIn("MARKETING_REQUIRES_OPT_IN", msg.last_error)

        contact.record_opt_in("manual")
        OutboundMessage.objects.filter(pk=msg.pk).update(
            status=OutboundMessage.Status.QUEUED, attempts=0, next_attempt_at=None
        )
        drain_outbound_queue()
        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.SENT)
