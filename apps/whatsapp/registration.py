"""Cloud API registration — the single path for OAuth, manual and retry."""
import logging
import secrets
from dataclasses import dataclass

from apps.whatsapp.embedded import EmbeddedSignupError, register_phone_number
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

logger = logging.getLogger(__name__)

R = WhatsAppBusinessNumber.RegistrationStatus


@dataclass(frozen=True)
class RegistrationResult:
    ok: bool
    already_registered: bool = False
    error: str = ""


def register_number(number: WhatsAppBusinessNumber) -> RegistrationResult:
    """Register ``number`` on the Cloud API. Idempotent and safe to retry.

    Persists the outcome on the number. Reuses the stored PIN when there is
    one: a number that already has two-step verification rejects a new PIN.
    """
    if number.registration_status == R.REGISTERED:
        return RegistrationResult(ok=True, already_registered=True)

    if not number.access_token:
        # Nothing to call Meta with; the user must supply credentials first.
        number.registration_status = R.PENDING
        number.registration_error = "No access token — reconnect WhatsApp or add a token."
        number.save(update_fields=["registration_status", "registration_error", "updated_at"])
        return RegistrationResult(ok=False, error=number.registration_error)

    number.registration_status = R.REGISTERING
    number.registration_error = ""
    number.save(update_fields=["registration_status", "registration_error", "updated_at"])

    pin = number.verification_pin or f"{secrets.randbelow(1_000_000):06d}"
    try:
        register_phone_number(number.phone_number_id, number.access_token, pin)
    except EmbeddedSignupError as exc:
        logger.warning("register_number: %s failed: %s", number.phone_number_id, exc)
        number.registration_status = R.FAILED
        number.registration_error = str(exc)
        number.save(update_fields=["registration_status", "registration_error", "updated_at"])
        return RegistrationResult(ok=False, error=str(exc))
    except Exception as exc:  # network errors etc. must not leave it REGISTERING
        logger.warning("register_number: %s errored: %s", number.phone_number_id, exc)
        number.registration_status = R.FAILED
        number.registration_error = "Could not reach Meta. Try again."
        number.save(update_fields=["registration_status", "registration_error", "updated_at"])
        return RegistrationResult(ok=False, error=number.registration_error)

    number.registration_status = R.REGISTERED
    number.registration_error = ""
    number.verification_pin = pin
    number.save(
        update_fields=["registration_status", "registration_error", "verification_pin", "updated_at"]
    )
    return RegistrationResult(ok=True)
