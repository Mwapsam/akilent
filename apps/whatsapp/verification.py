"""Verify-connection: send a test message and report a machine-readable result.

The frontend never parses Meta responses; it reads ``ok``, ``error_code``,
``message`` and ``action`` (one of ``retry_registration``, ``reconnect``,
``fix_number``, ``message_first``, ``retry``, ``contact_support``).
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.whatsapp.models.contact import normalize_phone
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.models.verification import ConnectionTest
from apps.whatsapp.providers.meta import MetaCloudAPIProvider

logger = logging.getLogger(__name__)

RETRY_REGISTRATION, RECONNECT, FIX_NUMBER, MESSAGE_FIRST, RETRY, CONTACT_SUPPORT = (
    "retry_registration", "reconnect", "fix_number", "message_first", "retry", "contact_support",
)

WINDOW = timedelta(hours=24)
TEXT_BODY = "Your WhatsApp connection is working. You can ignore this message."
AUTO_REPLY_BODY = (
    "✅ Your WhatsApp number is connected. Messages you send here will now appear in your inbox."
)
DELAYED_AFTER = timedelta(seconds=30)

# error code (before any "/subcode") -> (friendly message, action)
_ERRORS = {
    "133010": ("This number isn't registered on the WhatsApp Cloud API yet.", RETRY_REGISTRATION),
    "190": ("Meta rejected the access token. Reconnect WhatsApp to refresh it.", RECONNECT),
    "131030": ("That phone number isn't allowed to receive messages from this account yet. "
               "Add it as a test recipient in Meta, or use another number.", FIX_NUMBER),
    "131026": ("WhatsApp couldn't deliver to that number. Check it has WhatsApp.", FIX_NUMBER),
    # hello_world is only deliverable from Meta's Public Test Numbers; a real
    # number needs the recipient to message first (free text) or its own template.
    "131058": ("Meta only allows its built-in test template from its public test numbers. "
               "Message this WhatsApp number from your phone first, then send the test again.",
               MESSAGE_FIRST),
    "132000": ("The test template isn't available on your WhatsApp account.", CONTACT_SUPPORT),
    "132001": ("The test template isn't available on your WhatsApp account.", CONTACT_SUPPORT),
}


def _fail(error_code: str, message: str, action: str) -> dict:
    return {"ok": False, "error_code": error_code, "message": message, "action": action}


def recent_inbound(number, recipient: str | None = None):
    """Latest inbound message inside the 24h window, optionally from one sender.

    Inside the window free text is allowed on any number, so this is both the
    "they messaged us first" check and proof that inbound webhooks work.
    """
    from apps.whatsapp.models.message import MessageLog

    qs = MessageLog.objects.filter(
        account=number.account,
        direction=MessageLog.Direction.INBOUND,
        timestamp__gte=timezone.now() - WINDOW,
    )
    if recipient:
        qs = qs.filter(contact__phone_number=recipient)
    return qs.select_related("contact").order_by("-timestamp").first()


def _window_open(number, recipient: str) -> bool:
    return recent_inbound(number, recipient) is not None


_SAFE_BUTTONS = {"QUICK_REPLY", "PHONE_NUMBER", "URL"}


def _is_parameterless(tpl: dict) -> bool:
    """Approved utility template that can be sent with no components/variables."""
    if (tpl.get("status") or "").upper() != "APPROVED":
        return False
    if (tpl.get("category") or "").upper() != "UTILITY":
        return False
    for c in tpl.get("components") or []:
        kind = (c.get("type") or "").upper()
        if kind == "HEADER":
            if (c.get("format") or "TEXT").upper() != "TEXT" or "{{" in (c.get("text") or ""):
                return False
        elif kind == "BODY":
            if "{{" in (c.get("text") or ""):
                return False
        elif kind == "BUTTONS":
            for btn in c.get("buttons") or []:
                if (btn.get("type") or "").upper() not in _SAFE_BUTTONS or "{{" in (btn.get("url") or ""):
                    return False
        elif kind not in ("FOOTER",):
            return False
    return bool(tpl.get("name") and tpl.get("language"))


def _pick_template(number, provider) -> tuple[str, str]:
    """Template for the test send.

    Explicit setting wins. Otherwise ask Meta for *this number's own WABA*
    (the source of truth: language codes and variables are exact, and local
    synced rows can belong to another WABA) and use an approved utility template
    with no variables. Falls back to ``hello_world``, which Meta only delivers
    from its public test numbers.
    """
    configured = getattr(settings, "WHATSAPP_VERIFY_TEMPLATE", "")
    if configured:
        return configured, getattr(settings, "WHATSAPP_VERIFY_TEMPLATE_LANG", "en_US")

    try:
        templates = provider.list_templates(number.waba_id) if number.waba_id else []
    except Exception as exc:  # a lookup failure must not block the test
        logger.warning("verify: could not list templates for %s: %s", number.waba_id, exc)
        templates = []
    for tpl in templates:
        if _is_parameterless(tpl):
            return tpl["name"], tpl["language"]
    return "hello_world", "en_US"


def verify_connection(
    number: WhatsAppBusinessNumber, recipient_raw: str, *, body: str = TEXT_BODY
) -> dict:
    if not number.is_ready:
        return _fail(
            "not_registered",
            "Finish registering this number before sending a test message.",
            RETRY_REGISTRATION,
        )
    try:
        recipient = normalize_phone((recipient_raw or "").strip())
    except ValidationError:
        return _fail("invalid_number", "Enter a valid phone number with country code.", FIX_NUMBER)

    provider = MetaCloudAPIProvider(number.access_token, number.phone_number_id)
    if _window_open(number, recipient):
        # The recipient messaged us in the last 24h, so free text is allowed and
        # works on any number (no template needed).
        result = provider.send_text(recipient, body)
    else:
        name, language = _pick_template(number, provider)
        result = provider.send_template(recipient, name, language, [])

    if result.success:
        ConnectionTest.objects.create(
            number=number, recipient=recipient,
            status=ConnectionTest.Status.SENT, message_id=result.message_id,
        )
        return {"ok": True, "message_id": result.message_id, "status": "sent"}

    code = str(result.error_code or "")
    ConnectionTest.objects.create(
        number=number, recipient=recipient, status=ConnectionTest.Status.FAILED,
        error_code=code[:32], error=(result.error or "")[:2000],
    )
    friendly, action = _ERRORS.get(
        code.split("/")[0],
        ("Meta couldn't send the test message. Try again in a moment.", RETRY),
    )
    if action == RETRY_REGISTRATION:
        # Meta says it isn't registered: reflect that so the Retry button shows.
        number.registration_status = number.RegistrationStatus.FAILED
        number.registration_error = f"Meta error {code}: not registered on the Cloud API."
        number.save(update_fields=["registration_status", "registration_error", "updated_at"])
    return _fail(code or "send_failed", friendly, action)


def maybe_auto_reply(number: WhatsAppBusinessNumber, sender_phone: str):
    """Answer the first message a new number receives, so setup confirms itself.

    Proves both directions at once (their message reached us, our reply reaches
    them) and records a successful ``ConnectionTest``. Only during setup: once
    a test has succeeded this never replies again. Returns the result or None.
    """
    if not number.is_ready or number.last_successful_test() is not None:
        return None
    return verify_connection(number, sender_phone, body=AUTO_REPLY_BODY)


def inbound_stage(number: WhatsAppBusinessNumber, since: float | None = None) -> dict:
    """Where the "message us first" step stands, with a user-facing message.

    ``stage`` is one of waiting / processing / delayed / failed / received.
    Uses the raw webhook log (written synchronously) so the user gets feedback
    even when message processing is slow or failing.
    """
    from datetime import datetime, timezone as dt_tz

    from apps.whatsapp.models.webhook import WebhookEventLog

    inbound = recent_inbound(number)
    if inbound is not None:
        sender = inbound.contact.phone_number
        replied = number.last_successful_test() is not None
        return {
            "stage": "received", "sender": sender,
            "message": f"Message received from {sender}."
            + (" We sent a confirmation reply — check your phone." if replied else ""),
        }

    started = (
        datetime.fromtimestamp(since, tz=dt_tz.utc) if since else timezone.now() - timedelta(minutes=10)
    )
    event = (
        WebhookEventLog.objects.filter(
            event_type="message",
            created_at__gte=started,
            payload__entry__0__changes__0__value__metadata__phone_number_id=number.phone_number_id,
        )
        .order_by("-created_at")
        .first()
    )
    if event is None:
        return {"stage": "waiting", "sender": "", "message": "Waiting for your message…"}
    if event.error_message or event.attempts:
        return {
            "stage": "failed", "sender": "",
            "message": "Your message reached us but we couldn't process it yet. We'll keep "
                       "retrying — if this doesn't clear, contact support.",
        }
    if not event.processed and timezone.now() - event.created_at > DELAYED_AFTER:
        return {
            "stage": "delayed", "sender": "",
            "message": "Your message reached us, but processing is taking longer than usual…",
        }
    return {"stage": "processing", "sender": "",
            "message": "We heard from Meta — processing your message…"}
