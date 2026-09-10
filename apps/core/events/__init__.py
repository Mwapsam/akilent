"""
Event dispatcher for Akilent domain events.

This module provides the EventDispatcher abstraction that all internal
modules use to publish and subscribe to domain events. Today it's backed
by Django signals; it can be swapped for a durable broker (Redis, Kafka)
later without any module changes.

Usage:

    from apps.core.events import dispatcher, MessageReceived

    # Publish a domain event
    dispatcher.publish(MessageReceived(
        account_id=123,
        contact_id=456,
        message_id="wamid_789",
        body="Hello",
        occurred_at=timezone.now(),
    ))

    # Subscribe to domain events
    def handle_message_received(event: MessageReceived):
        # Do something with the event
        pass

    dispatcher.subscribe(MessageReceived, handle_message_received)
"""
from .dispatcher import DjangoSignalDispatcher
from .domain_events import BusinessEventReceived, MessageReceived, MessageStatusChanged

# Singleton dispatcher instance — all modules import and use this.
dispatcher = DjangoSignalDispatcher()

__all__ = [
    "dispatcher",
    "MessageReceived",
    "MessageStatusChanged",
    "BusinessEventReceived",
]
