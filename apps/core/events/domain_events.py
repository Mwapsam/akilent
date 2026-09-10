"""
Domain event definitions for Akilent.

Domain events represent business facts that have occurred, independent of any
vendor or implementation detail. They are published by domain modules (WhatsApp,
Email, Payments) and subscribed to by optional/intelligence services
(Automation, AI, Analytics).

When an Integration Event arrives from a vendor (e.g. Meta's webhook payload),
it's translated into a Domain Event at the module boundary, so internal code
never sees vendor-specific shapes.

All domain events MUST be immutable (frozen dataclasses).
"""
from dataclasses import dataclass
from datetime import datetime

from apps.core.events.dispatcher import DomainEvent


@dataclass(frozen=True)
class MessageReceived(DomainEvent):
    """A message was received from a WhatsApp contact.

    This event is published by apps/whatsapp when an inbound message webhook
    is processed. It's the primary trigger for automation rules and AI responses.

    Attributes:
        account_id: The Akilent tenant (Account) this message belongs to.
        contact_id: The WhatsAppContact who sent the message.
        message_id: The Meta message ID (wamid_...).
        channel: The channel the message came through (always "whatsapp" for now).
        body: The message text content (empty string for media-only messages).
        message_type: The type of message (text, image, audio, video, document, location, etc.).
        occurred_at: When the event occurred (from the webhook timestamp).
    """

    account_id: int
    contact_id: int
    message_id: str
    channel: str
    body: str
    message_type: str
    occurred_at: datetime


@dataclass(frozen=True)
class MessageStatusChanged(DomainEvent):
    """A message's delivery/read status changed.

    This event is published by apps/whatsapp when a status update webhook
    is processed (sent, delivered, read, failed).

    Attributes:
        account_id: The Akilent tenant (Account) this message belongs to.
        message_id: The Meta message ID (wamid_...).
        status: The new status (sent, delivered, read, failed).
        occurred_at: When the event occurred (from the webhook timestamp).
    """

    account_id: int
    message_id: str
    status: str
    occurred_at: datetime


@dataclass(frozen=True)
class BusinessEventReceived(DomainEvent):
    """A developer-supplied business fact arrived via POST /v1/events.

    Attributes:
        account_id: The Akilent tenant.
        event_id: BusinessEvent.public_id.
        name: Dotted event name, e.g. "invoice.paid".
        contact_id: The resolved Contact id, or None.
        data: The event payload.
        occurred_at: When the event happened (client-supplied or receipt time).
    """

    account_id: int
    event_id: str
    name: str
    contact_id: int | None
    data: dict
    occurred_at: datetime
