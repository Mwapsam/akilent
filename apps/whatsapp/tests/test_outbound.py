"""Phase 1: outbound send path — OutboundMessage → provider → MessageLog mirror.

Locks down the repaired outbound pipeline:

* ``api.send_message`` builds a valid ``payload`` dict and an idempotency key.
* ``drain_outbound_queue`` mirrors every send into a ``MessageLog``
  (direction=out) so status webhooks can reconcile.
* provider failure re-queues with backoff; a spent message goes FAILED.
* duplicate idempotency keys are cancelled, not re-sent.
* a message stuck in SENDING past the stale window is recovered.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.whatsapp import api as whatsapp_api
from apps.whatsapp.models import Conversation, MessageLog, OutboundMessage
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.tasks import drain_outbound_queue
from apps.whatsapp.types import SendResult


class _FakeProvider:
    """Stand-in for MetaCloudAPIProvider; records calls, returns canned results."""

    def __init__(self, result=None, exc=None):
        self._result = result or SendResult(message_id="wamid.OUT1", success=True)
        self._exc = exc
        self.calls = []

    def send_text(self, to, body):
        self.calls.append(("text", to, body))
        if self._exc:
            raise self._exc
        return self._result

    def send_template(self, to, name, language, components):
        self.calls.append(("template", to, name))
        if self._exc:
            raise self._exc
        return self._result


class OutboundSendTest(TestCase):
    def setUp(self):
        # settings_test runs Celery eager, so send_message()'s
        # drain_outbound_queue.delay() would fire a real send inline. Silence the
        # auto-drain; each test drives drain_outbound_queue() explicitly.
        patcher = patch("apps.whatsapp.tasks.drain_outbound_queue.delay")
        self.addCleanup(patcher.stop)
        patcher.start()

        self.account = Account.objects.create(company_name="Test Co", slug="test-co")
        WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="123456789",
            waba_id="waba_123",
            access_token="tok",
            is_active=True,
        )
        self.contact = whatsapp_api.get_or_create_contact(
            self.account, "+260971234567", "Test User"
        )
        # Open the 24h customer-service window so free-text sends are authorized.
        self.conversation = Conversation.get_or_open(self.contact)
        self.conversation.register_inbound(timezone.now())

    def _drain_with(self, provider):
        with patch(
            "apps.whatsapp.tasks._get_provider_for_account", return_value=provider
        ):
            drain_outbound_queue()

    # --- api.send_message -------------------------------------------------

    def test_send_message_builds_payload_and_idempotency_key(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi there")
        msg.refresh_from_db()
        self.assertEqual(msg.payload, {"type": "text", "body": "hi there"})
        self.assertTrue(msg.idempotency_key)
        self.assertEqual(msg.status, OutboundMessage.Status.QUEUED)

    # --- happy path -----------------------------------------------------

    def test_drain_sends_and_mirrors_to_message_log(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        provider = _FakeProvider()
        self._drain_with(provider)

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.SENT)
        self.assertIsNotNone(msg.sent_at)
        self.assertEqual(provider.calls, [("text", "+260971234567", "hi")])

        log = msg.message_log
        self.assertIsNotNone(log)
        self.assertEqual(log.direction, MessageLog.Direction.OUTBOUND)
        self.assertEqual(log.message_id, "wamid.OUT1")
        self.assertEqual(log.status, MessageLog.Status.SENT)
        self.assertEqual(log.content, "hi")

    def test_status_webhook_reconciles_against_outbound_log(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        self._drain_with(_FakeProvider())
        log = MessageLog.objects.get(message_id="wamid.OUT1")

        # Simulate _handle_status_update's core lookup + ranking.
        fetched = MessageLog.objects.get(
            message_id="wamid.OUT1", direction=MessageLog.Direction.OUTBOUND
        )
        self.assertEqual(fetched.pk, log.pk)
        self.assertTrue(fetched.apply_status_update("delivered"))
        self.assertTrue(fetched.apply_status_update("read"))
        self.assertFalse(fetched.apply_status_update("sent"))  # monotonic

    def test_successful_send_does_not_extend_24h_window(self):
        window_before = Conversation.objects.get(
            contact=self.contact
        ).window_expires_at
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        self._drain_with(_FakeProvider())
        convo = Conversation.objects.get(contact=self.contact)
        # last_message_at advances, but the window itself is untouched by an
        # agent-initiated message (only inbound extends it).
        self.assertEqual(convo.window_expires_at, window_before)

    # --- failure handling ---------------------------------------------

    def test_provider_failure_requeues_with_backoff(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        self._drain_with(_FakeProvider(exc=RuntimeError("boom")))

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.QUEUED)
        self.assertEqual(msg.attempts, 1)
        self.assertIsNotNone(msg.next_attempt_at)
        self.assertIn("boom", msg.last_error)

    def test_send_failure_result_marks_failed_after_max_attempts(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        msg.attempts = OutboundMessage.MAX_ATTEMPTS - 1
        msg.save(update_fields=["attempts"])

        bad = SendResult(message_id="", success=False, error="invalid_recipient")
        self._drain_with(_FakeProvider(result=bad))

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.FAILED)
        self.assertEqual(msg.message_log.status, MessageLog.Status.FAILED)

    def test_permanent_failure_notifies_automation_reconciliation_hook(self):
        """A terminal FAILED transition must call the apps.automation hook.

        apps.whatsapp doesn't know about workflows — it just calls the hook
        with the message; apps.automation.integrations.whatsapp owns what
        happens next (see apps/automation/tests/test_workflow_engine.py for
        the WorkflowStepRun/WorkflowRun reconciliation behavior itself).
        """
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        msg.attempts = OutboundMessage.MAX_ATTEMPTS - 1
        msg.save(update_fields=["attempts"])

        bad = SendResult(message_id="", success=False, error="invalid_recipient")
        with patch(
            "apps.automation.integrations.whatsapp.mark_outbound_message_failed"
        ) as hook:
            self._drain_with(_FakeProvider(result=bad))

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.FAILED)
        hook.assert_called_once_with(msg)

    def test_retryable_failure_does_not_notify_reconciliation_hook(self):
        """A requeued (non-terminal) failure must not fire the hook yet."""
        whatsapp_api.send_message(self.account, self.contact, "hi")
        with patch(
            "apps.automation.integrations.whatsapp.mark_outbound_message_failed"
        ) as hook:
            self._drain_with(_FakeProvider(exc=RuntimeError("boom")))

        hook.assert_not_called()

    # --- idempotency & recovery -------------------------------------

    def test_duplicate_idempotency_key_returns_existing_message(self):
        first = whatsapp_api.send_message(
            self.account, self.contact, "first", idempotency_key="dup-1"
        )
        second = whatsapp_api.send_message(
            self.account, self.contact, "second", idempotency_key="dup-1"
        )
        # Same row returned; the second call did not create or mutate anything.
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.payload["body"], "first")
        self.assertEqual(
            OutboundMessage.objects.filter(idempotency_key="dup-1").count(), 1
        )

        provider = _FakeProvider()
        self._drain_with(provider)
        self.assertEqual(len(provider.calls), 1)

    def test_non_retryable_provider_error_fails_immediately(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        bad = SendResult(
            message_id="", success=False, error="Template error",
            error_code="132001", retryable=False,
        )
        self._drain_with(_FakeProvider(result=bad))

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.FAILED)
        self.assertEqual(msg.error_code, "132001")
        self.assertEqual(msg.attempts, 1)  # did not burn through the retry budget
        self.assertEqual(msg.message_log.status, MessageLog.Status.FAILED)

    def test_retryable_provider_error_requeues(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        bad = SendResult(
            message_id="", success=False, error="Rate limited",
            error_code="130429", retryable=True,
        )
        self._drain_with(_FakeProvider(result=bad))

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.QUEUED)
        self.assertEqual(msg.error_code, "130429")
        self.assertIsNotNone(msg.next_attempt_at)

    def test_stale_sending_message_is_recovered(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        OutboundMessage.objects.filter(pk=msg.pk).update(
            status=OutboundMessage.Status.SENDING,
            updated_at=timezone.now() - timedelta(minutes=30),
        )
        provider = _FakeProvider()
        self._drain_with(provider)

        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.SENT)
        self.assertEqual(len(provider.calls), 1)
