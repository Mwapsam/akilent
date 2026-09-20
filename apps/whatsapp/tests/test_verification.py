"""Verify-connection: test send, structured results, and derived setup status."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.whatsapp.models import Conversation, MessageLog
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber as N
from apps.whatsapp.models.verification import ConnectionTest
from apps.whatsapp.numbers import numbers_verify
from apps.whatsapp.types import SendResult
from apps.whatsapp.verification import verify_connection

R = N.RegistrationStatus
S = N.SetupStatus
SEND = "apps.whatsapp.verification.MetaCloudAPIProvider.send_template"
LIST = "apps.whatsapp.verification.MetaCloudAPIProvider.list_templates"
TESTER = "+260971234567"


def ok(mid="wamid.1"):
    return SendResult(message_id=mid, success=True)


def fail(code, msg="boom"):
    return SendResult(message_id="", success=False, error=msg, error_code=code, retryable=False)


class VerifyBase(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")
        self.number = N.objects.create(
            account=self.account, phone_number_id="PNID", access_token="tok",
            waba_id="W", registration_status=R.REGISTERED,
        )
        patcher = patch(LIST, return_value=[])
        self.list_templates = patcher.start()
        self.addCleanup(patcher.stop)

    def inbound(self, phone, when=None):
        contact = self.account.contacts.create(phone_number=phone)
        convo = Conversation.get_or_open(contact)
        return MessageLog.objects.create(
            account=self.account, conversation=convo, contact=contact,
            direction=MessageLog.Direction.INBOUND, message_type="text", content="hi",
            status="delivered", timestamp=when or timezone.now(),
        )


class VerifyConnectionTest(VerifyBase):
    def test_success_records_test_and_returns_contract(self):
        with patch(SEND, return_value=ok()) as send:
            r = verify_connection(self.number, "+260 97 1234567")
        self.assertEqual(r, {"ok": True, "message_id": "wamid.1", "status": "sent"})
        self.assertEqual(send.call_args[0][0], TESTER)
        t = self.number.connection_tests.get()
        self.assertEqual((t.status, t.recipient), ("sent", TESTER))

    def test_unregistered_number_is_not_sent(self):
        self.number.registration_status = R.FAILED
        self.number.save()
        with patch(SEND) as send:
            r = verify_connection(self.number, TESTER)
        send.assert_not_called()
        self.assertEqual((r["ok"], r["action"]), (False, "retry_registration"))

    def test_invalid_number(self):
        with patch(SEND) as send:
            r = verify_connection(self.number, "abc")
        send.assert_not_called()
        self.assertEqual((r["error_code"], r["action"]), ("invalid_number", "fix_number"))

    def test_133010_flips_number_to_failed_and_offers_retry(self):
        with patch(SEND, return_value=fail("133010")):
            r = verify_connection(self.number, TESTER)
        self.assertEqual((r["ok"], r["error_code"], r["action"]), (False, "133010", "retry_registration"))
        self.number.refresh_from_db()
        self.assertEqual(self.number.registration_status, R.FAILED)
        self.assertFalse(self.number.is_ready)
        self.assertEqual(self.number.connection_tests.get().status, "failed")

    def test_token_error_offers_reconnect(self):
        with patch(SEND, return_value=fail("190")):
            self.assertEqual(verify_connection(self.number, TESTER)["action"], "reconnect")

    def test_missing_template_offers_support(self):
        with patch(SEND, return_value=fail("132001")):
            self.assertEqual(verify_connection(self.number, TESTER)["action"], "contact_support")

    def test_unknown_error_is_generic_retry(self):
        with patch(SEND, return_value=fail("999/1")):
            r = verify_connection(self.number, TESTER)
        self.assertEqual((r["action"], r["error_code"]), ("retry", "999/1"))


class TemplateChoiceTest(VerifyBase):
    def test_recent_inbound_from_recipient_sends_free_text(self):
        self.inbound(TESTER)
        with patch(SEND) as tpl, patch(
            "apps.whatsapp.verification.MetaCloudAPIProvider.send_text", return_value=ok("wamid.t")
        ) as text:
            r = verify_connection(self.number, TESTER)
        tpl.assert_not_called()
        text.assert_called_once()
        self.assertTrue(r["ok"])

    def test_old_inbound_does_not_open_window(self):
        self.inbound(TESTER, when=timezone.now() - timedelta(hours=30))
        with patch(SEND, return_value=ok()) as tpl:
            verify_connection(self.number, TESTER)
        tpl.assert_called_once()

    def _tpl(self, name, *, lang="en_US", status="APPROVED", category="UTILITY", components=None):
        return {"name": name, "language": lang, "status": status, "category": category,
                "components": components if components is not None else [{"type": "BODY", "text": "Thanks"}]}

    def test_uses_meta_language_of_an_approved_parameterless_template(self):
        self.list_templates.return_value = [
            self._tpl("with_var", components=[{"type": "BODY", "text": "Hi {{1}}"}]),
            self._tpl("pending_one", status="PENDING"),
            self._tpl("marketing_one", category="MARKETING"),
            self._tpl("plain", lang="en_GB"),
        ]
        with patch(SEND, return_value=ok()) as tpl:
            verify_connection(self.number, TESTER)
        self.assertEqual(tpl.call_args[0][1:3], ("plain", "en_GB"))
        self.list_templates.assert_called_once_with("W")  # this number's own WABA

    def test_skips_templates_needing_media_header_or_dynamic_button(self):
        self.list_templates.return_value = [
            self._tpl("img", components=[{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "x"}]),
            self._tpl("btn", components=[{"type": "BODY", "text": "x"},
                                         {"type": "BUTTONS", "buttons": [{"type": "URL", "url": "https://a/{{1}}"}]}]),
            self._tpl("otp", components=[{"type": "BODY", "text": "x"},
                                         {"type": "BUTTONS", "buttons": [{"type": "COPY_CODE"}]}]),
        ]
        with patch(SEND, return_value=ok()) as tpl:
            verify_connection(self.number, TESTER)
        self.assertEqual(tpl.call_args[0][1], "hello_world")

    def test_template_lookup_failure_falls_back_to_hello_world(self):
        self.list_templates.side_effect = RuntimeError("meta down")
        with patch(SEND, return_value=ok()) as tpl:
            verify_connection(self.number, TESTER)
        self.assertEqual(tpl.call_args[0][1], "hello_world")

    def test_setting_overrides_lookup(self):
        with self.settings(WHATSAPP_VERIFY_TEMPLATE="mine", WHATSAPP_VERIFY_TEMPLATE_LANG="fr"), patch(
            SEND, return_value=ok()
        ) as tpl:
            verify_connection(self.number, TESTER)
        self.assertEqual(tpl.call_args[0][1:3], ("mine", "fr"))
        self.list_templates.assert_not_called()

    def test_falls_back_to_hello_world(self):
        with patch(SEND, return_value=ok()) as tpl:
            verify_connection(self.number, TESTER)
        self.assertEqual(tpl.call_args[0][1:3], ("hello_world", "en_US"))

    def test_131058_tells_user_to_message_first(self):
        with patch(SEND, return_value=fail("131058")):
            r = verify_connection(self.number, TESTER)
        self.assertEqual((r["ok"], r["action"]), (False, "message_first"))
        self.assertIn("Message this WhatsApp number", r["message"])


class SetupStatusTest(VerifyBase):
    def test_lifecycle(self):
        self.assertEqual(self.number.setup_status, S.READY_FOR_TEST)
        with patch(SEND, return_value=ok()):
            verify_connection(self.number, TESTER)
        self.assertEqual(self.number.setup_status, S.TEST_SENT)
        self.inbound(TESTER)
        self.assertEqual(self.number.setup_status, S.READY)

    def test_unrelated_inbound_does_not_complete_setup(self):
        with patch(SEND, return_value=ok()):
            verify_connection(self.number, TESTER)
        self.inbound("+260977000111")
        self.assertEqual(self.number.setup_status, S.TEST_SENT)

    def test_reply_before_the_test_does_not_count(self):
        self.inbound(TESTER, when=timezone.now() - timedelta(days=1))
        with patch(SEND, return_value=ok()):
            verify_connection(self.number, TESTER)
        self.assertEqual(self.number.setup_status, S.TEST_SENT)

    def test_failed_test_does_not_advance(self):
        with patch(SEND, return_value=fail("999")):
            verify_connection(self.number, TESTER)
        self.assertEqual(self.number.setup_status, S.READY_FOR_TEST)

    def test_revoked_token_after_test_is_not_stale(self):
        with patch(SEND, return_value=ok()):
            verify_connection(self.number, TESTER)
        self.number.access_token = None
        self.number.save()
        self.assertEqual(self.number.setup_status, S.ACTION_REQUIRED)


class VerifyViewTest(VerifyBase):
    def setUp(self):
        super().setUp()
        self.other = Account.objects.create(company_name="Other", slug="other")
        self.user = User.objects.create_user("u", password="p")

    def _post(self, pk, recipient=TESTER):
        request = RequestFactory().post(f"/whatsapp/numbers/{pk}/verify/", {"recipient": recipient})
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        with patch("apps.whatsapp.numbers.get_current_account", return_value=self.account):
            return numbers_verify(request, pk)

    def test_success_json(self):
        with patch(SEND, return_value=ok()):
            r = self._post(self.number.pk)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content)["status"], "sent")

    def test_failure_json_is_machine_readable(self):
        with patch(SEND, return_value=fail("133010")):
            r = self._post(self.number.pk)
        body = json.loads(r.content)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(body), {"ok", "error_code", "message", "action"})

    def test_other_accounts_number_404(self):
        from django.http import Http404

        foreign = N.objects.create(account=self.other, phone_number_id="X", access_token="t", waba_id="W")
        with self.assertRaises(Http404):
            self._post(foreign.pk)
