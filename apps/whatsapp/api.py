import logging
import uuid
from typing import Optional

from django.core.exceptions import ObjectDoesNotExist

from apps.accounts.models import Account
from apps.whatsapp.models import (
    MessageLog,
    OutboundMessage,
    WebhookEventLog,
    WhatsAppContact,
    WhatsAppBusinessNumber,
)

logger = logging.getLogger(__name__)


# --- Contact access (for automation, queries) ---


def get_contact(account: Account, contact_id: int) -> WhatsAppContact:
    """Get a WhatsApp contact by ID.

    Raises:
        WhatsAppContact.DoesNotExist: if contact not found
    """
    return WhatsAppContact.objects.get(id=contact_id, account=account)


def get_contact_by_phone(account: Account, phone_number: str) -> Optional[WhatsAppContact]:
    """Get a WhatsApp contact by phone number, or None if not found."""
    return WhatsAppContact.objects.filter(
        account=account, phone_number=phone_number
    ).first()


def get_or_create_contact(
    account: Account, phone_number: str, display_name: Optional[str] = None
) -> WhatsAppContact:
    """Get or create a WhatsApp contact.

    Args:
        account: The account that owns this contact
        phone_number: Phone number in E.164 format
        display_name: Optional display name for the contact
    """
    contact, _ = WhatsAppContact.objects.get_or_create(
        account=account,
        phone_number=phone_number,
        defaults={"display_name": display_name},
    )
    return contact


# --- Message sending ---


def send_message(
    account: Account,
    contact: WhatsAppContact,
    text: str,
    message_type: str = "text",
    *,
    idempotency_key: Optional[str] = None,
) -> OutboundMessage:
    """Send a free-text WhatsApp message to a contact.

    This is the public interface for outbound messaging. It creates a QUEUED
    OutboundMessage carrying a provider-agnostic ``payload`` dict (the shape
    consumed by ``apps.whatsapp.tasks._send_outbound``) and enqueues a drain run.

    Args:
        account: The account sending the message
        contact: The recipient contact
        text: Message text/body
        message_type: Message type (default: 'text')
        idempotency_key: Optional caller-supplied dedupe key. When omitted a
            random key is generated so the ``unique_outbound_idempotency``
            constraint always has a value to enforce.

    Returns:
        The created OutboundMessage instance

    Raises:
        Account.DoesNotExist: if account is not valid
    """
    key = idempotency_key or uuid.uuid4().hex
    msg, created = OutboundMessage.objects.get_or_create(
        account=account,
        idempotency_key=key,
        defaults={
            "contact": contact,
            "payload": {"type": message_type, "body": text},
        },
    )
    if not created:
        # A message with this idempotency key already exists — return it
        # unchanged rather than double-sending.
        return msg

    # Enqueue for delivery
    from apps.whatsapp.tasks import drain_outbound_queue
    drain_outbound_queue.delay()
    return msg


def send_interactive(
    account: Account,
    contact: WhatsAppContact,
    interactive: dict,
    *,
    idempotency_key: Optional[str] = None,
) -> OutboundMessage:
    """Queue reply buttons or a list for a contact.

    ``interactive`` comes from ``apps.whatsapp.interactive.build_buttons`` / ``build_list``.
    It goes through the same outbound queue as free text, so consent, opt-out, the 24-hour
    window and rate limits all apply.
    """
    from apps.whatsapp import interactive as interactive_messages

    body = interactive["body"]["text"]
    key = idempotency_key or uuid.uuid4().hex
    msg, created = OutboundMessage.objects.get_or_create(
        account=account,
        idempotency_key=key,
        defaults={
            "contact": contact,
            "payload": {
                "type": "interactive",
                "body": body,
                "interactive": interactive,
                "options": interactive_messages.option_titles(interactive),
            },
        },
    )
    if not created:
        return msg

    from apps.whatsapp.tasks import drain_outbound_queue
    drain_outbound_queue.delay()
    return msg


# --- Webhook event access---


def get_webhook_event(event_id: int, source: Optional[str] = None) -> WebhookEventLog:
    filters = {"pk": event_id}
    if source:
        filters["source"] = source
    return WebhookEventLog.objects.get(**filters)


# --- Billing/limits access ---


def count_active_business_numbers(account: Account) -> int:
    """Count active WhatsApp business numbers for this account.

    Used by billing to enforce plan limits.
    """
    return WhatsAppBusinessNumber.objects.filter(
        account=account, is_active=True
    ).count()


def outbound_queue_depth(account: Optional[Account] = None) -> int:
    """Number of OutboundMessages still waiting to be sent.

    Platform-wide by default; pass an account to scope it. Cheap gauge for a
    health/admin view.
    """
    qs = OutboundMessage.objects.filter(status=OutboundMessage.Status.QUEUED)
    if account is not None:
        qs = qs.filter(account=account)
    return qs.count()


def count_conversations(account: Account) -> int:
    """Count active conversations for this account.

    Used by billing to enforce plan limits on conversation capacity.
    """
    from apps.whatsapp.models import Conversation
    return Conversation.objects.filter(
        contact__account=account, is_open=True
    ).count()
