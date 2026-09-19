"""Verify-connection: send a test message and report a machine-readable result.

The frontend never parses Meta responses; it reads ``ok``, ``error_code``,
``message`` and ``action`` (one of ``retry_registration``, ``reconnect``,
``fix_number``, ``retry``, ``contact_support``).
"""
import logging

from django.conf import settings
from django.core.exceptions import ValidationError

from apps.whatsapp.models.contact import normalize_phone
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.models.verification import ConnectionTest
from apps.whatsapp.providers.meta import MetaCloudAPIProvider

logger = logging.getLogger(__name__)

RETRY_REGISTRATION, RECONNECT, FIX_NUMBER, RETRY, CONTACT_SUPPORT = (
    "retry_registration", "reconnect", "fix_number", "retry", "contact_support",
)

# error code (before any "/subcode") -> (friendly message, action)
_ERRORS = {
    "133010": ("This number isn't registered on the WhatsApp Cloud API yet.", RETRY_REGISTRATION),
    "190": ("Meta rejected the access token. Reconnect WhatsApp to refresh it.", RECONNECT),
    "131030": ("That phone number isn't allowed to receive messages from this account yet. "
               "Add it as a test recipient in Meta, or use another number.", FIX_NUMBER),
    "131026": ("WhatsApp couldn't deliver to that number. Check it has WhatsApp.", FIX_NUMBER),
    "132000": ("The test template isn't available on your WhatsApp account.", CONTACT_SUPPORT),
    "132001": ("The test template isn't available on your WhatsApp account.", CONTACT_SUPPORT),
}


def _fail(error_code: str, message: str, action: str) -> dict:
    return {"ok": False, "error_code": error_code, "message": message, "action": action}


def verify_connection(number: WhatsAppBusinessNumber, recipient_raw: str) -> dict:
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
    result = provider.send_template(
        recipient,
        getattr(settings, "WHATSAPP_VERIFY_TEMPLATE", "hello_world"),
        getattr(settings, "WHATSAPP_VERIFY_TEMPLATE_LANG", "en_US"),
        [],
    )

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
