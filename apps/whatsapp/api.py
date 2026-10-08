import logging
import uuid

from apps.accounts.models import Account
from apps.whatsapp.models import (
    OutboundMessage,
    WebhookEventLog,
    WhatsAppBusinessNumber,
    WhatsAppContact,
)

logger = logging.getLogger(__name__)


# --- Contact access (for automation, queries) ---


def get_contact(account: Account, contact_id: int) -> WhatsAppContact:
    """Get a WhatsApp contact by ID.

    Raises:
        WhatsAppContact.DoesNotExist: if contact not found
    """
    return WhatsAppContact.objects.get(id=contact_id, account=account)


def get_contact_by_phone(account: Account, phone_number: str) -> WhatsAppContact | None:
    """Get a WhatsApp contact by phone number, or None if not found."""
    return WhatsAppContact.objects.filter(
        account=account, phone_number=phone_number
    ).first()


def get_or_create_contact(
    account: Account, phone_number: str, display_name: str | None = None
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
    idempotency_key: str | None = None,
    sent_by: str = "",
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
        sent_by: "automation", "ai" or "system" when no person wrote it, so the inbox and
            Insights credit the reply correctly. Empty for a person's message.

    Returns:
        The created OutboundMessage instance

    Raises:
        Account.DoesNotExist: if account is not valid
    """
    key = idempotency_key or uuid.uuid4().hex
    payload = {"type": message_type, "body": text}
    if sent_by:
        payload["_sent_by"] = sent_by
    msg, created = OutboundMessage.objects.get_or_create(
        account=account,
        idempotency_key=key,
        defaults={"contact": contact, "payload": payload},
    )
    if not created:
        # A message with this idempotency key already exists — return it
        # unchanged rather than double-sending.
        return msg

    # Enqueue for delivery
    from apps.whatsapp.tasks import drain_outbound_queue

    drain_outbound_queue.delay()
    return msg


def send_media(
    account: Account,
    contact: WhatsAppContact,
    *,
    media_path: str,
    mime_type: str,
    kind: str,
    caption: str = "",
    filename: str = "",
    conversation_id: int | None = None,
    idempotency_key: str | None = None,
    sent_by: str = "",
) -> OutboundMessage:
    """Queue a photo, video, document or voice note already saved in our storage.

    ``kind`` is image / video / audio / document. The drain uploads the file to Meta and
    sends it, through the same consent, opt-out and 24-hour-window checks as text.
    """
    key = idempotency_key or uuid.uuid4().hex
    payload: dict[str, str | int] = {
        "type": kind,
        "media_path": media_path,
        "mime_type": mime_type,
        "caption": caption if kind in ("image", "video", "document") else "",
    }
    if kind == "document":
        payload["filename"] = filename or media_path.rsplit("/", 1)[-1]
    if conversation_id:
        payload["_conversation_id"] = conversation_id
    if sent_by:
        payload["_sent_by"] = sent_by
    msg, created = OutboundMessage.objects.get_or_create(
        account=account,
        idempotency_key=key,
        defaults={"contact": contact, "payload": payload},
    )
    if not created:
        return msg

    from apps.whatsapp.tasks import drain_outbound_queue

    drain_outbound_queue.delay()
    return msg


def send_interactive(
    account: Account,
    contact: WhatsAppContact,
    interactive: dict,
    *,
    idempotency_key: str | None = None,
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


def get_webhook_event(event_id: int, source: str | None = None) -> WebhookEventLog:
    filters: dict = {"pk": event_id}
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


def outbound_queue_depth(account: Account | None = None) -> int:
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

    return Conversation.objects.filter(contact__account=account, is_open=True).count()


def find_contact_by_phone(account: Account, phone: str) -> WhatsAppContact | None:
    """The account's WhatsApp contact for ``phone``, ignoring "+", spaces and dashes."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 7:
        return None
    for contact in WhatsAppContact.objects.filter(
        account=account, phone_number__contains=digits[-7:]
    ):
        if "".join(ch for ch in contact.phone_number if ch.isdigit()) == digits:
            return contact
    return None


def free_text_window_is_open(contact: WhatsAppContact) -> bool:
    """Whether a normal (non-template) message may be sent to ``contact`` right now."""
    from apps.whatsapp.models import Conversation

    conversation = (
        Conversation.objects.filter(contact=contact)
        .order_by("-last_message_at", "-id")
        .first()
    )
    return bool(conversation and conversation.window_is_open)


def automations_start_from_messages() -> bool:
    """Whether a customer's WhatsApp message may start automations at all (a site-wide switch)."""
    from apps.whatsapp.tasks import _automation_events_enabled

    try:
        return bool(_automation_events_enabled())
    except Exception:
        return False


def approved_template_by_name(account: Account, name: str):
    """The account's approved template with this WhatsApp name, or None."""
    from apps.whatsapp.models import MessageTemplate

    if not name:
        return None
    return MessageTemplate.objects.filter(
        account=account,
        whatsapp_template_name=name,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    ).first()


def first_approved_template(account: Account):
    """The account's first approved template by name (for a sensible default), or None."""
    from apps.whatsapp.models import MessageTemplate

    return (
        MessageTemplate.objects.filter(
            account=account,
            approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        )
        .exclude(whatsapp_template_name__isnull=True)
        .order_by("name")
        .first()
    )


def template_counts(account: Account) -> dict:
    """``{"total", "approved", "pending", "rejected"}`` for the account's WhatsApp templates."""
    from django.db.models import Count

    from apps.whatsapp.models import MessageTemplate

    rows = dict(
        MessageTemplate.objects.filter(account=account)
        .values_list("approval_status")
        .annotate(n=Count("id"))
        .values_list("approval_status", "n")
    )
    return {
        "total": sum(rows.values()),
        "approved": rows.get("approved", 0),
        "pending": rows.get("pending", 0),
        "rejected": rows.get("rejected", 0),
    }


def validate_template_fields(**fields) -> str:
    """The first problem with a template's fields as ``template_builder`` sees it, or "" if valid."""
    from apps.whatsapp.template_builder import TemplateBuilderError, validate_fields

    try:
        validate_fields(**fields)
    except TemplateBuilderError as exc:
        return str(exc)
    return ""


def lint_template(**fields) -> list[dict]:
    """Likely reasons Meta would reject or re-classify a template (see ``template_lint``)."""
    from apps.whatsapp.template_lint import lint

    return lint(**fields)


def import_templates(account: Account) -> dict:
    """Pull the business's templates from Meta now (the "Sync now" button). ``{"synced", "errors"}``."""
    from apps.whatsapp.tasks import sync_templates_for_account

    return sync_templates_for_account(account)


# --- Operations (the Pilot Command Center and the Starting point) ---


def connected_since(account: Account):
    """When this business first connected WhatsApp, or None if it has no active number now.

    Counted from its first number ever, not the earliest still active: a business that replaced
    its number connected when it first did, not when it swapped.
    """
    numbers = WhatsAppBusinessNumber.objects.filter(account=account)
    if not numbers.filter(is_active=True).exists():
        return None
    return numbers.order_by("created_at").values_list("created_at", flat=True).first()


def connected_accounts() -> list[tuple]:
    """``[(account, connected_since)]`` for every business with an active WhatsApp number."""
    from django.db.models import Min

    active = WhatsAppBusinessNumber.objects.filter(is_active=True).values("account")
    rows = (
        WhatsAppBusinessNumber.objects.filter(account__in=active)
        .values("account")
        .annotate(since=Min("created_at"))
        .order_by("since")
    )
    accounts = Account.objects.in_bulk([r["account"] for r in rows])
    return [
        (accounts[r["account"]], r["since"]) for r in rows if r["account"] in accounts
    ]


def ops_status() -> dict:
    """Platform-wide WhatsApp health for staff: webhooks arriving, sends draining, sends failing."""
    from datetime import timedelta

    from django.utils import timezone

    now = timezone.now()
    events = WebhookEventLog.objects.filter(source=WebhookEventLog.Source.WHATSAPP)
    last = events.order_by("-created_at").values_list("created_at", flat=True).first()
    oldest = (
        OutboundMessage.objects.filter(status=OutboundMessage.Status.QUEUED)
        .order_by("created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    return {
        "last_webhook_at": last,
        "unprocessed_webhooks": events.filter(processed=False).count(),
        "queued": OutboundMessage.objects.filter(
            status=OutboundMessage.Status.QUEUED
        ).count(),
        "oldest_queued_minutes": int((now - oldest).total_seconds() // 60)
        if oldest
        else None,
        "failed_24h": OutboundMessage.objects.filter(
            status=OutboundMessage.Status.FAILED,
            updated_at__gte=now - timedelta(hours=24),
        ).count(),
    }


# --- One-time codes (see verification_codes.py) ---


def send_verification_code(
    account: Account,
    *,
    phone: str,
    code: str,
    template_name: str = "",
    language: str = "",
    idempotency_key: str = "",
    dry_run: bool = False,
) -> dict:
    """Deliver a one-time code another system made. Raises ``VerificationCodeError``."""
    from apps.whatsapp import verification_codes

    return verification_codes.send_code(
        account,
        phone=phone,
        code=code,
        template_name=template_name,
        language=language,
        idempotency_key=idempotency_key,
        dry_run=dry_run,
    )


def verification_code_status(account: Account, message_id: str) -> dict | None:
    """Where a code sent by ``send_verification_code`` is (queued, sent, delivered, read, failed), or None."""
    from apps.whatsapp import verification_codes

    msg = verification_codes.get_message(account, message_id)
    return verification_codes.status_of(msg) if msg else None


def authentication_templates(account: Account) -> list:
    """The account's approved Authentication templates, newest first."""
    from apps.whatsapp import verification_codes

    return verification_codes.approved_templates(account)
