"""Sending one-time codes (login, sign-up, payment confirmation) with an Authentication template.

Another system — the business's own app or website — creates the code and asks Akilent to deliver
it on WhatsApp (``POST /api/v1/whatsapp/verification-codes``). Akilent never makes or checks codes;
it only delivers them, so a code is kept only until it is sent and then blanked out.

Rules that differ from other template sends:
- The person asked for the code, so it goes out even if they once replied STOP to the business.
- A code that couldn't be sent before it expires is dropped rather than retried late.
- It stays out of the inbox: it's a system message, not a conversation with the business.
- At most ``MAX_PER_HOUR`` codes go to one number, so a leaked key can't be used to flood people.
"""
from __future__ import annotations

import re
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.whatsapp.models import MessageTemplate, OutboundMessage, WhatsAppContact
from apps.whatsapp.models.contact import normalize_phone
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

KIND = "verification_code"
KEY_PREFIX = "otp:"
MAX_PER_HOUR = 5
DEFAULT_EXPIRY_MINUTES = 10
HIDDEN = "••••••"

_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,15}$")
_EXPIRY_RE = re.compile(r"expires in (\d+) minute", re.IGNORECASE)


class VerificationCodeError(Exception):
    """Why a code can't be sent. ``code`` is stable for API callers; the message is plain words."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(message)


def is_verification_code(payload: dict | None) -> bool:
    return (payload or {}).get("kind") == KIND


def _templates(account):
    return MessageTemplate.objects.filter(
        account=account,
        category=MessageTemplate.Category.AUTHENTICATION,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    ).order_by("-created_at", "-pk")


def approved_templates(account) -> list:
    return list(_templates(account))


def choose_template(account, name: str = "", language: str = ""):
    templates = _templates(account)
    if name:
        templates = templates.filter(whatsapp_template_name=name)
    if language:
        templates = templates.filter(language_code=language)
    template = templates.first()
    if template is not None:
        return template
    if name or language:
        wanted = " ".join(filter(None, [repr(name) if name else "", f"in {language!r}" if language else ""]))
        raise VerificationCodeError(
            "template_not_found",
            f"There's no approved Authentication template {wanted}. "
            "Check the name and language on the WhatsApp templates page.",
            status=404,
        )
    raise VerificationCodeError(
        "no_authentication_template",
        "This business has no approved Authentication template yet. "
        "Create one on the WhatsApp templates page and wait for Meta to approve it.",
        status=409,
    )


def expiry_minutes(template) -> int:
    match = _EXPIRY_RE.search(template.footer or "")
    return int(match.group(1)) if match else DEFAULT_EXPIRY_MINUTES


def components(code: str) -> list[dict]:
    """Meta's send shape for an authentication template: the code fills the body and the button."""
    return [
        {"type": "body", "parameters": [{"type": "text", "text": code}]},
        {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": code}]},
    ]


def _check(account, phone: str, code: str):
    if not WhatsAppBusinessNumber.objects.filter(account=account, is_active=True).exists():
        raise VerificationCodeError(
            "whatsapp_not_connected", "This business has no connected WhatsApp number.", status=409,
        )
    code = (code or "").strip()
    if not _CODE_RE.match(code):
        raise VerificationCodeError(
            "invalid_code", "The code must be 4 to 15 letters or numbers, with no spaces.",
        )
    try:
        phone = normalize_phone(phone or "")
    except ValidationError:
        raise VerificationCodeError(
            "invalid_phone", "The phone number isn't valid. Send it with the country code, like +260971234567.",
        )
    return phone, code


def send_code(account, *, phone: str, code: str, template_name: str = "", language: str = "",
              idempotency_key: str = "", dry_run: bool = False) -> dict:
    """Queue ``code`` for ``phone``. Returns the same dict ``status_of`` does.

    ``dry_run`` checks everything (for test API keys) but sends nothing.
    """
    phone, code = _check(account, phone, code)
    template = choose_template(account, template_name, language)
    minutes = expiry_minutes(template)
    now = timezone.now()

    contact = WhatsAppContact.objects.filter(account=account, phone_number=phone).first()
    if contact is not None:
        recent = OutboundMessage.objects.filter(
            account=account, contact=contact, payload__kind=KIND,
            created_at__gte=now - timedelta(hours=1),
        ).count()
        if recent >= MAX_PER_HOUR:
            raise VerificationCodeError(
                "too_many_codes",
                f"{MAX_PER_HOUR} codes were already sent to this number in the last hour. Try again later.",
                status=429,
            )

    if dry_run:
        return {"id": None, "status": "test", "to": phone, "template": template.whatsapp_template_name,
                "language": template.language_code, "expires_in_minutes": minutes, "test": True}

    if contact is None:
        contact, _ = WhatsAppContact.objects.get_or_create(account=account, phone_number=phone)
    key = KEY_PREFIX + (idempotency_key.strip()[:200] if idempotency_key else uuid.uuid4().hex)
    msg, created = OutboundMessage.objects.get_or_create(
        account=account,
        idempotency_key=key,
        defaults={
            "contact": contact,
            "template": template,
            "payload": {
                "type": "template",
                "kind": KIND,
                "template_name": template.whatsapp_template_name,
                "language": template.language_code,
                "components": components(code),
                "params": {"Verification code": HIDDEN},
                "expires_at": (now + timedelta(minutes=minutes)).isoformat(),
            },
        },
    )
    if created:
        from apps.whatsapp.tasks import drain_outbound_queue
        drain_outbound_queue.delay()
    return status_of(msg)


def is_expired(payload: dict) -> bool:
    raw = (payload or {}).get("expires_at")
    if not raw:
        return False
    from django.utils.dateparse import parse_datetime
    expires = parse_datetime(raw)
    return expires is not None and timezone.now() > expires


def forget_code(msg) -> None:
    """Blank the code once it no longer needs sending: nobody at the business should read it later."""
    for owner, field in ((msg, "payload"), (getattr(msg, "message_log", None), "raw_payload")):
        payload = getattr(owner, field, None) if owner is not None else None
        if not is_verification_code(payload):
            continue
        payload = dict(payload)
        payload["components"] = components(HIDDEN)
        setattr(owner, field, payload)
        owner.save(update_fields=[field])


def status_of(msg) -> dict:
    from apps.whatsapp.friendly_errors import friendly_send_error

    log = msg.message_log
    status = msg.status
    if log is not None and msg.status == OutboundMessage.Status.SENT:
        status = log.status  # sent → delivered → read, from Meta's status webhooks
    body = {
        "id": str(msg.pk),
        "status": status,
        "to": msg.contact.phone_number,
        "template": (msg.payload or {}).get("template_name", ""),
        "language": (msg.payload or {}).get("language", ""),
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
    }
    if msg.status == OutboundMessage.Status.FAILED:
        body["error"] = {"code": msg.error_code or (msg.last_error or "").split(":")[0],
                         "message": friendly_send_error(msg.error_code or (msg.last_error or "").split(":")[0])}
    return body


def get_message(account, message_id: str):
    if not str(message_id).isdigit():
        return None
    return OutboundMessage.objects.filter(
        account=account, pk=int(message_id), payload__kind=KIND,
    ).select_related("contact", "message_log").first()
