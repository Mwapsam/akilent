"""The MessageReceived bridge and webhook signature diagnostics.

- MessageReceived carries the real wamid, so the automation bridge finds the MessageLog.
- A WhatsAppContact pk in MessageReceived is never looked up as a canonical Contact pk
  (that matched whichever unrelated customer happened to share the number).
- A rejected signature says which Meta account sent it, so a delivery from an app whose
  secret this server doesn't hold is diagnosable from the log alone.
"""

import json
from io import BytesIO
from unittest.mock import patch

from django.http import HttpRequest
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.core.events import MessageReceived, dispatcher
from apps.whatsapp.models import WhatsAppContact
from apps.whatsapp.services.inbound import WhatsAppInboundService
from apps.whatsapp.views import WhatsAppWebhookView


class MessageReceivedCarriesWamidTest(TestCase):
    def test_publish_includes_message_id(self):
        account = Account.objects.create(company_name="A", slug="a-wamid")
        wa = WhatsAppContact.objects.create(
            account=account, phone_number="+260970000001"
        )
        with (
            patch("apps.whatsapp.tasks._automation_events_enabled", return_value=True),
            patch.object(dispatcher, "publish") as publish,
        ):
            WhatsAppInboundService._publish_message_received(
                account, wa, timezone.now(), "text", "hi", message_id="wamid.ABC"
            )
        event = publish.call_args.args[0]
        self.assertEqual(event.message_id, "wamid.ABC")


class WhatsAppContactIdIsNotAContactIdTest(TestCase):
    def test_unrelated_contact_with_matching_pk_is_not_used(self):
        from apps.automation.triggers import on_message_received

        account = Account.objects.create(company_name="B", slug="b-ids")
        linked = Contact.objects.create(account=account, phone="+260970000002")
        # A different customer whose Contact pk equals the WhatsAppContact pk.
        unrelated = Contact.objects.create(account=account, phone="+260979999999")
        wa = WhatsAppContact.objects.create(
            pk=unrelated.pk,
            account=account,
            phone_number="+260970000002",
            contact=linked,
        )
        assert wa.pk == unrelated.pk != linked.pk

        event = MessageReceived(
            account_id=account.pk,
            contact_id=wa.pk,
            message_id="wamid.X",
            channel="whatsapp",
            body="hello",
            message_type="text",
            occurred_at=timezone.now(),
        )
        with (
            patch("apps.automation.triggers._enroll_workflows_for_reply"),
            patch("apps.automation.tasks.evaluate_rules_for_message.delay") as delay,
        ):
            on_message_received(event)
        context = delay.call_args.args[2]
        self.assertEqual(context["phone_number"], "+260970000002")


@override_settings(WHATSAPP_APP_SECRET="right_secret")
class SignatureFailureLoggingTest(TestCase):
    def _post(self, payload):
        body = json.dumps(payload).encode()
        request = HttpRequest()
        request.method = "POST"
        request._stream = BytesIO(body)
        request._body = body
        request.META = {
            "CONTENT_TYPE": "application/json",
            "HTTP_X_HUB_SIGNATURE_256": "sha256=" + "0" * 64,
            "HTTP_USER_AGENT": "facebookexternalua",
            "REMOTE_ADDR": "1.2.3.4",
        }
        return WhatsAppWebhookView().post(request)

    def test_rejection_names_the_sending_account(self):
        with self.assertLogs("apps.core.meta_signature", level="ERROR") as logs:
            response = self._post({"entry": [{"id": "WABA123", "changes": []}]})
        self.assertEqual(response.status_code, 403)
        self.assertIn("entry_id=WABA123", logs.output[0])
        self.assertNotIn("right_secret", logs.output[0])

    @override_settings(WHATSAPP_APP_SECRET="")
    def test_missing_secret_is_reported_distinctly(self):
        with self.assertLogs("apps.whatsapp.views", level="ERROR") as logs:
            response = self._post({"entry": []})
        self.assertEqual(response.status_code, 403)
        self.assertIn("not configured", logs.output[0])
