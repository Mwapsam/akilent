"""First inbound message on a new number: logged reliably, and answered so the
user sees the connection work. Plus the staged feedback the setup step polls."""
import json
import time
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.whatsapp import numbers as numbers_views
from apps.whatsapp.models import MessageLog, WebhookEventLog, WhatsAppContact
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber as N
from apps.whatsapp.tasks import _handle_inbound_message
from apps.whatsapp.types import SendResult
from apps.whatsapp.verification import AUTO_REPLY_BODY, inbound_stage, maybe_auto_reply

R = N.RegistrationStatus
WA_ID = "260971903744"  # how Meta sends it: no "+"
PHONE = "+260971903744"
SEND_TEXT = "apps.whatsapp.verification.MetaCloudAPIProvider.send_text"


def payload(text="Hi", wa_id=WA_ID, msg_id="wamid.1", pnid="PNID"):
    return {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": pnid},
        "contacts": [{"profile": {"name": "Tester"}}],
        "messages": [{"from": wa_id, "id": msg_id, "timestamp": str(int(time.time())),
                      "type": "text", "text": {"body": text}}],
    }}]}]}


class Base(TestCase):
    def setUp(self):
        from apps.billing.models import Plan, Subscription

        self.account = Account.objects.create(company_name="Co", slug="co")
        plan = Plan.objects.first() or Plan.objects.create(
            name="Test Plan", slug="test-plan", price_monthly=0, max_whatsapp_numbers=5)
        Subscription.objects.create(account=self.account, plan=plan, status=Subscription.ACTIVE,
                                    current_period_start=timezone.now())
        self.number = N.objects.create(
            account=self.account, phone_number_id="PNID", access_token="tok", waba_id="W",
            registration_status=R.REGISTERED,
        )
        for target in ("apps.whatsapp.tasks.mark_read", "apps.whatsapp.tasks.dispatcher",
                       "apps.whatsapp.tasks.drain_outbound_queue"):
            p = patch(target)
            p.start()
            self.addCleanup(p.stop)

    def receive(self, **kw):
        event = WebhookEventLog.objects.create(
            source="whatsapp", event_type="message", payload=payload(**kw))
        _handle_inbound_message(event)
        return event


class InboundContactTest(Base):
    def test_existing_contact_is_matched_despite_missing_plus(self):
        WhatsAppContact.objects.create(account=self.account, phone_number=PHONE)
        with patch(SEND_TEXT, return_value=SendResult(message_id="m", success=True)):
            self.receive()
        self.assertEqual(MessageLog.objects.filter(direction="in").count(), 1)
        self.assertEqual(WhatsAppContact.objects.filter(account=self.account).count(), 1)

    def test_new_contact_is_stored_normalized(self):
        with patch(SEND_TEXT, return_value=SendResult(message_id="m", success=True)):
            self.receive()
        self.assertEqual(WhatsAppContact.objects.get().phone_number, PHONE)


class AutoReplyTest(Base):
    def _ok(self):
        return patch(SEND_TEXT, return_value=SendResult(message_id="wamid.out", success=True))

    def test_first_message_gets_a_confirmation_and_records_the_test(self):
        with self._ok() as send:
            self.receive()
        send.assert_called_once_with(PHONE, AUTO_REPLY_BODY)
        test = self.number.connection_tests.get()
        self.assertEqual((test.status, test.recipient), ("sent", PHONE))
        self.assertIsNotNone(self.number.last_successful_test())

    def test_only_replies_once_during_setup(self):
        with self._ok() as send:
            self.receive(msg_id="wamid.1")
            self.receive(msg_id="wamid.2")
        self.assertEqual(send.call_count, 1)

    def test_replayed_event_does_not_reply_again(self):
        with self._ok() as send:
            self.receive(msg_id="wamid.1")
            self.number.connection_tests.all().delete()  # even if the test were gone
            self.receive(msg_id="wamid.1")  # same message id => not "created"
        self.assertEqual(send.call_count, 1)

    def test_stop_keyword_is_not_answered(self):
        with self._ok() as send:
            self.receive(text="STOP")
        send.assert_not_called()

    def test_failed_reply_never_breaks_inbound_processing(self):
        fail = SendResult(message_id="", success=False, error="x", error_code="131058", retryable=False)
        with patch(SEND_TEXT, return_value=fail):
            self.receive()
        self.assertEqual(MessageLog.objects.filter(direction="in").count(), 1)
        self.assertEqual(self.number.connection_tests.get().status, "failed")

    def test_exception_in_reply_is_swallowed(self):
        with patch(SEND_TEXT, side_effect=RuntimeError("boom")):
            self.receive()
        self.assertEqual(MessageLog.objects.filter(direction="in").count(), 1)

    def test_no_reply_when_number_not_ready(self):
        self.number.registration_status = R.FAILED
        self.number.save()
        with self._ok() as send:
            self.receive()
        send.assert_not_called()

    def test_maybe_auto_reply_skips_once_tested(self):
        self.number.connection_tests.create(recipient=PHONE, status="sent")
        with self._ok() as send:
            self.assertIsNone(maybe_auto_reply(self.number, PHONE))
        send.assert_not_called()


class InboundStageTest(Base):
    def _event(self, *, pnid="PNID", ago=timedelta(seconds=5), **fields):
        e = WebhookEventLog.objects.create(
            source="whatsapp", event_type="message", payload=payload(pnid=pnid), **fields)
        WebhookEventLog.objects.filter(pk=e.pk).update(created_at=timezone.now() - ago)
        return e

    def test_waiting_when_nothing_arrived(self):
        s = inbound_stage(self.number)
        self.assertEqual(s["stage"], "waiting")

    def test_processing_when_webhook_seen_but_not_yet_logged(self):
        self._event()
        self.assertEqual(inbound_stage(self.number)["stage"], "processing")

    def test_delayed_when_unprocessed_for_a_while(self):
        self._event(ago=timedelta(minutes=2))
        self.assertEqual(inbound_stage(self.number)["stage"], "delayed")

    def test_failed_when_processing_errored(self):
        self._event(attempts=1, error_message="boom")
        s = inbound_stage(self.number)
        self.assertEqual(s["stage"], "failed")
        self.assertIn("couldn't process", s["message"])

    def test_other_numbers_events_are_ignored(self):
        self._event(pnid="OTHER")
        self.assertEqual(inbound_stage(self.number)["stage"], "waiting")

    def test_events_before_the_step_started_are_ignored(self):
        self._event(ago=timedelta(minutes=5))
        self.assertEqual(inbound_stage(self.number, since=time.time())["stage"], "waiting")

    def test_received_mentions_the_reply_when_sent(self):
        with patch(SEND_TEXT, return_value=SendResult(message_id="m", success=True)):
            self.receive()
        s = inbound_stage(self.number)
        self.assertEqual((s["stage"], s["sender"]), ("received", PHONE))
        self.assertIn("confirmation reply", s["message"])


class StatusEndpointStageTest(Base):
    def test_contract(self):
        request = RequestFactory().get(f"/whatsapp/numbers/{self.number.pk}/status/", {"since": "abc"})
        request.user = User.objects.create_user("u", password="p")
        with patch("apps.whatsapp.numbers.get_current_account", return_value=self.account):
            body = json.loads(numbers_views.numbers_status(request, self.number.pk).content)
        self.assertEqual(body["stage"], "waiting")
        self.assertFalse(body["message_received"])
        self.assertTrue(body["message"])
