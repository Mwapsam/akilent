"""Cloud API registration — the single path for OAuth, manual and retry."""

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from apps.whatsapp.embedded import EmbeddedSignupError, register_phone_number
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

logger = logging.getLogger(__name__)

R = WhatsAppBusinessNumber.RegistrationStatus

# Meta error code for two-step verification PIN mismatch (#133005).
_PIN_MISMATCH = "133005"

# Progressive lockout delays: attempt 1 = none, 2 = 30s, 3 = 10min, 4+ = 1h
_LOCKOUT_SECONDS = [0, 30, 600, 3600]


def _lockout_seconds(attempts: int) -> int:
    if attempts <= 0:
        return 0
    idx = min(attempts - 1, len(_LOCKOUT_SECONDS) - 1)
    return _LOCKOUT_SECONDS[idx]


def is_pin_mismatch(error: str) -> bool:
    return _PIN_MISMATCH in error


@dataclass(frozen=True)
class RegistrationResult:
    ok: bool
    already_registered: bool = False
    pin_required: bool = False
    locked_until: datetime | None = None
    error: str = ""


def register_number(
    number: WhatsAppBusinessNumber, pin: str = ""
) -> RegistrationResult:
    """Register ``number`` on the Cloud API. Idempotent and safe to retry.

    Persists the outcome on the number. Pass ``pin`` to supply or override the
    stored two-step verification PIN. After a PIN-mismatch failure the caller
    must provide the correct PIN — a random PIN is never retried automatically.
    """
    if number.registration_status == R.REGISTERED:
        return RegistrationResult(ok=True, already_registered=True)

    # Check lockout before doing anything.
    if number.registration_locked_until:
        now = timezone.now()
        if now < number.registration_locked_until:
            return RegistrationResult(
                ok=False,
                locked_until=number.registration_locked_until,
                error=f"Too many failed attempts. Try again after {number.registration_locked_until.strftime('%H:%M UTC')}.",
            )

    if not number.access_token:
        number.registration_status = R.PENDING
        number.registration_error = (
            "No access token — reconnect WhatsApp or add a token."
        )
        number.save(
            update_fields=["registration_status", "registration_error", "updated_at"]
        )
        return RegistrationResult(ok=False, error=number.registration_error)

    # Resolve PIN: explicit arg > stored PIN > fresh random (first attempt only).
    # Once we've had a PIN-mismatch failure we never generate a random PIN again,
    # because the number already has a PIN and a new random one will always fail.
    had_previous_failure = number.registration_attempts > 0
    if pin:
        resolved_pin = pin
    elif number.verification_pin:
        resolved_pin = number.verification_pin
    elif had_previous_failure:
        # Don't waste an attempt with a random PIN — require user input.
        return RegistrationResult(
            ok=False,
            pin_required=True,
            error="Enter the two-step verification PIN configured for this WhatsApp number.",
        )
    else:
        resolved_pin = f"{secrets.randbelow(1_000_000):06d}"

    number.registration_status = R.REGISTERING
    number.registration_error = ""
    number.save(
        update_fields=["registration_status", "registration_error", "updated_at"]
    )

    try:
        register_phone_number(number.phone_number_id, number.access_token, resolved_pin)
    except EmbeddedSignupError as exc:
        error_str = str(exc)
        logger.warning("register_number: %s failed: %s", number.phone_number_id, exc)
        number.registration_attempts += 1
        delay = _lockout_seconds(number.registration_attempts)
        number.registration_locked_until = (
            timezone.now() + timedelta(seconds=delay) if delay else None
        )
        number.registration_status = R.FAILED
        number.registration_error = error_str
        number.save(
            update_fields=[
                "registration_status",
                "registration_error",
                "registration_attempts",
                "registration_locked_until",
                "updated_at",
            ]
        )
        pin_required = is_pin_mismatch(error_str)
        return RegistrationResult(
            ok=False,
            pin_required=pin_required,
            locked_until=number.registration_locked_until,
            error=error_str,
        )
    except Exception as exc:  # network errors etc. must not leave it REGISTERING
        logger.warning("register_number: %s errored: %s", number.phone_number_id, exc)
        number.registration_status = R.FAILED
        number.registration_error = "Could not reach Meta. Try again."
        number.save(
            update_fields=["registration_status", "registration_error", "updated_at"]
        )
        return RegistrationResult(ok=False, error=number.registration_error)

    number.registration_status = R.REGISTERED
    number.registration_error = ""
    number.registration_attempts = 0
    number.registration_locked_until = None
    number.verification_pin = resolved_pin
    number.save(
        update_fields=[
            "registration_status",
            "registration_error",
            "registration_attempts",
            "registration_locked_until",
            "verification_pin",
            "updated_at",
        ]
    )
    return RegistrationResult(ok=True)
