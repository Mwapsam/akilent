"""
Phase 1: Tests for event dispatcher and automation subscription.

These tests verify that:
1. The EventDispatcher abstraction exists and works
2. Domain events can be published and received
3. Automation rules subscribe to MessageReceived events
4. Event publishing doesn't break if there are no subscribers
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Account
from apps.core.events import MessageReceived, dispatcher
from apps.whatsapp.models import WhatsAppContact


class EventDispatcherTest(TestCase):
    """Test the EventDispatcher abstraction."""

    def test_dispatcher_publishes_and_subscribers_receive_events(self):
        """Test that events are published to subscribers."""
        received_events = []

        def subscriber(event: MessageReceived, **kwargs):
            received_events.append(event)

        # Subscribe to events
        dispatcher.subscribe(MessageReceived, subscriber)

        # Publish an event
        account = Account.objects.create(company_name="Test", slug="test")
        contact = WhatsAppContact.objects.create(
            account=account,
            phone_number="+260971234567",  # Valid Zambian number
        )
        event = MessageReceived(
            account_id=account.id,
            contact_id=contact.id,
            message_id="msg_123",
            channel="whatsapp",
            body="Hello",
            message_type="text",
            occurred_at=timezone.now(),
        )
        dispatcher.publish(event)

        # Verify subscriber received the event
        self.assertEqual(len(received_events), 1)
        self.assertEqual(received_events[0].message_id, "msg_123")
        self.assertEqual(received_events[0].body, "Hello")

    def test_dispatcher_catches_subscriber_exceptions(self):
        """Test that subscriber exceptions don't propagate to publisher."""

        def failing_subscriber(event: MessageReceived, **kwargs):
            raise ValueError("Subscriber failed!")

        def good_subscriber(event: MessageReceived, **kwargs):
            good_subscriber.called = True

        good_subscriber.called = False

        dispatcher.subscribe(MessageReceived, failing_subscriber)
        dispatcher.subscribe(MessageReceived, good_subscriber)

        account = Account.objects.create(company_name="Test", slug="test-2")
        contact = WhatsAppContact.objects.create(
            account=account,
            phone_number="+260971234568",  # Valid Zambian number
        )
        event = MessageReceived(
            account_id=account.id,
            contact_id=contact.id,
            message_id="msg_456",
            channel="whatsapp",
            body="World",
            message_type="text",
            occurred_at=timezone.now(),
        )

        # Publish should not raise, even though failing_subscriber raises
        try:
            dispatcher.publish(event)
        except Exception as e:
            self.fail(f"publish() should not raise, but raised: {e}")

        # The good subscriber should still have been called
        self.assertTrue(good_subscriber.called)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class AutomationSubscriberTest(TestCase):
    """Test that automation rules subscribe to domain events."""

    def test_automation_subscriber_is_registered(self):
        """Test that the automation trigger is registered as a subscriber."""
        # The subscriber should have been registered in AutomationConfig.ready()
        # We can verify this by checking that on_message_received is subscribed

        # Get the signal for MessageReceived events
        signal = dispatcher._get_signal(MessageReceived)
        receivers = signal.receivers or []

        # Check that on_message_received is in the receivers
        receiver_funcs = [
            r[1].__name__ if hasattr(r[1], "__name__") else str(r[1]) for r in receivers
        ]
        self.assertIn("on_message_received", receiver_funcs)

    def test_automation_receives_message_received_events(self):
        """Test that automation receives and processes MessageReceived events."""
        # Create a rule that matches our test message
        account = Account.objects.create(company_name="Test", slug="test-3")
        contact = WhatsAppContact.objects.create(
            account=account,
            phone_number="+260971234569",  # Valid Zambian number
        )

        from apps.automation.models import AutomationRule

        AutomationRule.objects.create(
            account=account,
            name="Test Rule",
            trigger_event=AutomationRule.TriggerEvent.MESSAGE_RECEIVED,
            conditions={"message_contains": "test"},
            action={"type": "unknown"},  # Use unknown action so it doesn't execute
            is_active=True,
        )

        # Mock the task dispatch to verify it's called
        with patch(
            "apps.automation.tasks.evaluate_rules_for_message.delay"
        ) as mock_delay:
            # Enable automation events
            from apps.core.models import SiteSettings

            settings = SiteSettings.load()
            settings.automation_events_enabled = True
            settings.save()

            # Publish a MessageReceived event
            event = MessageReceived(
                account_id=account.id,
                contact_id=contact.id,
                message_id="msg_test",
                channel="whatsapp",
                body="test message",
                message_type="text",
                occurred_at=timezone.now(),
            )
            dispatcher.publish(event)

            # Verify that the automation task was dispatched
            mock_delay.assert_called_once()
            call_args = mock_delay.call_args[0]
            self.assertEqual(call_args[0], account.id)  # account_id
            self.assertEqual(
                call_args[1], AutomationRule.TriggerEvent.MESSAGE_RECEIVED
            )  # trigger_event
            self.assertIn(
                "test message", str(call_args[2])
            )  # context contains the message
