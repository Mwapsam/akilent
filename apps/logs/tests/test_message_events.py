from unittest import mock

from django.test import TestCase

from apps.accounts.models import Account
from apps.email.models import EmailMessage
from apps.logs.models import MessageEvent
from apps.logs import services


class RecordMessageEventTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Acme")
        self.msg = EmailMessage.objects.create(
            account=self.account,
            from_email="billing@acme.test",
            to_email="user@example.com",
            subject="Invoice",
        )

    def test_append_and_status_reconcile(self):
        services.record_message_event(self.msg, "queued", source="pipeline")
        services.record_message_event(self.msg, "sent", source="pipeline")
        services.record_message_event(self.msg, "delivered", source="ses_sns")
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.status, EmailMessage.Status.DELIVERED)
        self.assertEqual(
            list(self.msg.events.values_list("type", flat=True)),
            ["queued", "sent", "delivered"],
        )

    def test_engagement_does_not_downgrade_terminal_status(self):
        services.record_message_event(self.msg, "bounced", source="ses_sns")
        services.record_message_event(self.msg, "opened", source="tracking_pixel")
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.status, EmailMessage.Status.BOUNCED)

    def test_delivered_does_not_override_bounce(self):
        services.record_message_event(self.msg, "bounced", source="ses_sns")
        services.record_message_event(self.msg, "delivered", source="ses_sns")
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.status, EmailMessage.Status.BOUNCED)

    def test_provider_event_id_dedupe(self):
        first = services.record_message_event(
            self.msg, "delivered", source="ses_sns", provider_event_id="sns-1"
        )
        dupe = services.record_message_event(
            self.msg, "delivered", source="ses_sns", provider_event_id="sns-1"
        )
        self.assertIsNotNone(first)
        self.assertIsNone(dupe)
        self.assertEqual(MessageEvent.objects.filter(message=self.msg).count(), 1)

    def test_failing_sink_does_not_break_write(self):
        with mock.patch.object(
            services, "_sink_webhooks", side_effect=RuntimeError("boom")
        ):
            event = services.record_message_event(self.msg, "sent", source="pipeline")
        self.assertIsNotNone(event)
        self.assertTrue(MessageEvent.objects.filter(pk=event.pk).exists())

    def test_webhook_sink_receives_mapped_event_name(self):
        with mock.patch("apps.email.webhooks.enqueue_event") as enqueue:
            services.record_message_event(self.msg, "bounced", source="ses_sns")
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0], "message.bounced")
