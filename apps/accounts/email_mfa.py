"""Email codes: a login factor for users who opt in, and a re-authentication step.

Codes are six digits, hashed with HMAC-SHA256 keyed on SECRET_KEY, single-use,
valid for ten minutes, and locked after five wrong guesses. Limits apply to the
whole account, not just one code: at most five codes are sent and ten guesses
are accepted in any hour. Each check locks the user row, so concurrent requests
cannot get around the limits. A refused request looks the same to the user, so
an attacker cannot tell which accounts exist or have codes.

An email code proves a login. It never counts as a strong factor for mandatory
MFA; see apps/accounts/mfa.py.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.models import EmailLoginCode, EmailMfaEnrolment

logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=10)
COOLDOWN = timedelta(seconds=60)
MAX_ATTEMPTS = 5  # per code
MAX_SENDS_PER_HOUR = 5  # per account
MAX_GUESSES_PER_HOUR = 10  # per account, across all codes

# Results of send_code.
SENT = "sent"
RECENT = "recent"  # cooldown or hourly limit: a code went out recently
UNAVAILABLE = "unavailable"  # no address on file, or delivery failed


def is_enrolled(user):
    return EmailMfaEnrolment.objects.filter(user=user).exists()


def _digest(user, purpose, code):
    secret = settings.SECRET_KEY
    if not secret:
        raise ImproperlyConfigured("SECRET_KEY is required for email codes.")
    message = f"{user.pk}:{purpose}:{code}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _lock_user(user):
    """Hold the user row for the rest of the transaction, serialising code requests."""
    get_user_model().objects.select_for_update().filter(pk=user.pk).first()


def send_code(user, purpose):
    """Email a fresh code. Returns SENT, RECENT or UNAVAILABLE."""
    from apps.email.services.send import send_system_email

    if not user.email:
        return UNAVAILABLE
    now = timezone.now()
    with transaction.atomic():
        _lock_user(user)
        latest = (
            EmailLoginCode.objects.filter(user=user, purpose=purpose)
            .order_by("-created_at")
            .first()
        )
        if latest is not None and now - latest.created_at < COOLDOWN:
            return RECENT
        recent = EmailLoginCode.objects.filter(
            user=user, created_at__gte=now - timedelta(hours=1)
        )
        if recent.count() >= MAX_SENDS_PER_HOUR:
            logger.warning("email_mfa: send limit reached for user %s", user.pk)
            return RECENT
        code = f"{secrets.randbelow(10**6):06d}"
        EmailLoginCode.objects.filter(
            user=user, purpose=purpose, used_at__isnull=True
        ).update(used_at=now)
        row = EmailLoginCode.objects.create(
            user=user,
            purpose=purpose,
            code_hash=_digest(user, purpose, code),
            expires_at=now + CODE_TTL,
        )
    try:
        send_system_email(
            to_email=user.email,
            subject="Your sign-in code",
            text_body=(
                f"Your code is {code}. It expires in 10 minutes. "
                "If you did not ask for it, ignore this email and change your password."
            ),
        )
    except Exception:
        # Nothing was delivered, so the code must not stay usable.
        logger.exception("email_mfa: could not send code to user %s", user.pk)
        # Delete the row: a failed send must not start a cooldown or use up the hourly limit.
        EmailLoginCode.objects.filter(pk=row.pk).delete()
        return UNAVAILABLE
    return SENT


def guess_limit_reached(user):
    """True when this account has used up its guesses for the hour."""
    spent = (
        EmailLoginCode.objects.filter(
            user=user, created_at__gte=timezone.now() - timedelta(hours=1)
        ).aggregate(total=Sum("attempts"))["total"]
        or 0
    )
    return spent >= MAX_GUESSES_PER_HOUR


def check_code(user, purpose, raw):
    """Return True only for the live, unused code. Every wrong guess counts."""
    raw = raw.replace(" ", "")
    now = timezone.now()
    with transaction.atomic():
        _lock_user(user)
        spent = (
            EmailLoginCode.objects.filter(
                user=user, created_at__gte=now - timedelta(hours=1)
            ).aggregate(total=Sum("attempts"))["total"]
            or 0
        )
        if spent >= MAX_GUESSES_PER_HOUR:
            logger.warning("email_mfa: guess limit reached for user %s", user.pk)
            return False
        row = (
            EmailLoginCode.objects.filter(
                user=user,
                purpose=purpose,
                used_at__isnull=True,
                expires_at__gt=now,
            )
            .order_by("-created_at")
            .first()
        )
        if row is None or row.attempts >= MAX_ATTEMPTS:
            return False
        row.attempts += 1
        if hmac.compare_digest(row.code_hash, _digest(user, purpose, raw)):
            row.used_at = now
            row.save(update_fields=["attempts", "used_at"])
            return True
        row.save(update_fields=["attempts"])
        return False
