"""Security notices to the account owner after a two-factor change.

A notice is sent only after the change has committed (transaction.on_commit), so a
rolled-back change never produces an email. The email names the change and the
time. It never contains a code, a recovery code or a secret.

A delivery failure is logged and recorded in the audit trail. It does not undo the
change, and it does not change what the user is told: the success message says the
change was made, and never says the email was sent.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.core.audit import platform_record

logger = logging.getLogger(__name__)

RECOVERY_REGENERATED = "recovery_regenerated"
TWO_FACTOR_DISABLED = "two_factor_disabled"

_COPY = {
    RECOVERY_REGENERATED: (
        "Your recovery codes were replaced",
        "Your two-factor recovery codes were replaced on {when}. Your old codes no longer work.",
    ),
    TWO_FACTOR_DISABLED: (
        "Two-factor authentication was turned off",
        "Two-factor authentication was turned off on your account on {when}. {tail}",
    ),
}

_FOOTER = (
    "If you didn't make this change, sign in and secure your account right away, "
    "then contact support."
)


def notify_after_commit(user, kind):
    """Schedule the owner's notice for after the current transaction commits."""
    transaction.on_commit(lambda: _send(user.pk, user.email, kind))


def _send(user_pk, email, kind):
    from apps.email.services.send import send_system_email

    if not email:
        return
    from apps.accounts.models import PasskeyCredential

    subject, template = _COPY[kind]
    when = timezone.now().strftime("%d %B %Y at %H:%M UTC")
    if (
        kind == TWO_FACTOR_DISABLED
        and PasskeyCredential.objects.filter(user_id=user_pk, confirmed=True).exists()
    ):
        tail = "Your passkeys and security keys still work."
    elif kind == TWO_FACTOR_DISABLED:
        tail = "Your password is now the only way to sign in."
    else:
        tail = ""
    body = f"{template.format(when=when, tail=tail)}\n\n{_FOOTER}\n"
    try:
        send_system_email(to_email=email, subject=subject, text_body=body)
    except Exception:
        # The change is already committed. Record the failure; do not reverse it.
        logger.exception("security notice %s failed for user %s", kind, user_pk)
        platform_record(
            action="auth.security_notice_failed",
            actor=None,
            success=False,
            metadata={"user_pk": user_pk, "notice": kind},
        )
