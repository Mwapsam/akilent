"""Email codes: a login factor for users who opt in, a re-authentication step, and the code
that confirms a new owner's email address after signup.

Codes are six digits, hashed with HMAC-SHA256 keyed on SECRET_KEY, single-use,
valid for ten minutes, and locked after five wrong guesses. Limits apply to the
whole account, not just one code: at most five codes are sent and ten guesses
are accepted in any hour. The sign-in purposes share that allowance; confirming
an email address has its own, so fumbling it can't lock anyone out of signing in
(and it gives no extra guesses at a sign-in code). Each check locks the user row,
so concurrent requests cannot get around the limits. A refused request looks the
same to the user, so an attacker cannot tell which accounts exist or have codes.

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
RECENT = "recent"  # cooldown: a code went out moments ago
CAPPED = "capped"  # hourly limit on emailed codes reached
UNAVAILABLE = "unavailable"  # no address on file, or delivery failed

# Stored until the task picks the code. Not hex, so no digest can ever equal it.
PENDING_HASH = "pending"


VERIFY = EmailLoginCode.Purpose.VERIFY


def _pool(purpose):
    """The purposes whose codes count toward the same hourly limits as ``purpose``."""
    if purpose == VERIFY:
        return [VERIFY]
    return [p for p in EmailLoginCode.Purpose.values if p != VERIFY]


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
    """Queue a fresh code. Returns SENT, RECENT, CAPPED or UNAVAILABLE.

    The row is created here, under the lock, so the cooldown and hourly limit hold. It
    starts with a hash that matches nothing; the Celery task picks the code, stores its
    hash and sends it. The page never waits on the mail server, and the plaintext code
    never passes through the broker.
    """
    from apps.accounts.tasks import send_email_code

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
            user=user,
            purpose__in=_pool(purpose),
            created_at__gte=now - timedelta(hours=1),
        )
        if recent.count() >= MAX_SENDS_PER_HOUR:
            logger.warning("email_mfa: send limit reached for user %s", user.pk)
            return CAPPED
        EmailLoginCode.objects.filter(
            user=user, purpose=purpose, used_at__isnull=True
        ).update(used_at=now)
        row = EmailLoginCode.objects.create(
            user=user,
            purpose=purpose,
            code_hash=PENDING_HASH,
            expires_at=now + CODE_TTL,
        )
    # Queued after the block so the worker sees the committed row.
    try:
        send_email_code.delay(row.pk)
    except Exception:
        # The broker is down (or, in eager mode, the send failed for good). Nothing will
        # be delivered: drop the row so it starts no cooldown and uses up no limit.
        logger.exception("email_mfa: could not send code to user %s", user.pk)
        EmailLoginCode.objects.filter(pk=row.pk).delete()
        return UNAVAILABLE
    return SENT


def deliver_code(row):
    """Pick the code for a queued row, store its hash and email it. Run by the task."""
    from apps.email.services.send import send_system_email

    code = f"{secrets.randbelow(10**6):06d}"
    row.code_hash = _digest(row.user, row.purpose, code)
    row.save(update_fields=["code_hash"])
    if row.purpose == VERIFY:
        subject, text_body, html_body = verify_email_parts(row.user, code)
        sender = "Account"
    else:
        subject, html_body, sender = CODE_SUBJECT, code_email_html(code), "Security"
        text_body = (
            f"Your code is {code}. It expires in 10 minutes. "
            "If you did not ask for it, ignore this email and change your password."
        )
    send_system_email(
        to_email=row.user.email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        sender_label=sender,
    )


VERIFY_SUBJECT = "Confirm your email address"


def verify_link(user) -> str:
    """The tokened confirmation link (apps.accounts.tokens), absolute on SITE_URL: the email
    is sent from a worker, with no request to build the host from."""
    from django.urls import reverse
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    from apps.accounts.tokens import email_verification_token

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    path = reverse(
        "verify_email",
        kwargs={"uidb64": uid, "token": email_verification_token.make_token(user)},
    )
    return settings.SITE_URL.rstrip("/") + path


def verify_email_parts(user, code: str) -> tuple[str, str, str]:
    """(subject, text, html) of the email that confirms a new owner's address: the code to
    type on the signup page, and the link as a fallback (another device, a lost tab)."""
    from apps.email.services.html_email import build_transactional_html

    link = verify_link(user)
    text = (
        f"Your code is {code}. Enter it on the page you signed up on; it expires in 10 "
        f"minutes.\n\nOr confirm with this link: {link}\n\n"
        "If you didn't create this account, ignore this email."
    )
    html = build_transactional_html(
        "Enter this code on the page you signed up on. It expires in 10 minutes.",
        VERIFY_SUBJECT,
        eyebrow="Account",
        heading="Confirm your email address",
        preheader=f"Your code is {code}.",
        code=code,
        cta_url=link,
        cta_label="Or confirm with one click",
        small_print="If you didn't create this account, you can ignore this email.",
    )
    return VERIFY_SUBJECT, text, html


CODE_SUBJECT = "Your sign-in code"


def code_email_html(code: str) -> str:
    """The branded HTML body of the sign-in code email (also used by the console preview)."""
    from apps.email.services.html_email import build_transactional_html

    return build_transactional_html(
        "Enter this code to finish signing in. It expires in 10 minutes.",
        CODE_SUBJECT,
        eyebrow="Security",
        heading="Your sign-in code",
        preheader="Your code expires in 10 minutes.",
        code=code,
        small_print=(
            "Never share this code. Akilent will never ask you for it. If you didn't "
            "try to sign in, ignore this email and change your password."
        ),
    )


def guess_limit_reached(user, purpose=EmailLoginCode.Purpose.LOGIN):
    """True when this account has used up its guesses for the hour (for ``purpose``'s pool)."""
    spent = (
        EmailLoginCode.objects.filter(
            user=user,
            purpose__in=_pool(purpose),
            created_at__gte=timezone.now() - timedelta(hours=1),
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
                user=user,
                purpose__in=_pool(purpose),
                created_at__gte=now - timedelta(hours=1),
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
