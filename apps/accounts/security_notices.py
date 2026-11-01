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
PASSKEY_ADDED = "passkey_added"

_COPY = {
    RECOVERY_REGENERATED: (
        "Your recovery codes were replaced",
        "Your two-factor recovery codes were replaced on {when}. Your old codes no longer work.",
    ),
    PASSKEY_ADDED: (
        "A passkey was added to your account",
        "A passkey was added to your account on {when}. If you didn't add it, sign in and "
        "remove it, then change your password.{tail}",
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


def notice_html(kind, *, when, tail=""):
    """The branded HTML body of one notice (also used by the console preview)."""
    from apps.email.services.html_email import (
        build_transactional_html,
        site_email_context,
    )

    subject, template = _COPY[kind]
    return build_transactional_html(
        template.format(when=when, tail=tail),
        subject,
        eyebrow="Security notice",
        heading=subject,
        preheader=f"This change was made on {when}.",
        details=[("When", when)],
        notice=_FOOTER,
        notice_tone="warning",
        cta_url=f"{site_email_context()['base_url']}/settings/security/",
        cta_label="Review security settings",
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
    tail = ""
    if kind == TWO_FACTOR_DISABLED:
        from apps.accounts.models import EmailMfaEnrolment

        if PasskeyCredential.objects.filter(user_id=user_pk, confirmed=True).exists():
            tail = "Your passkeys and security keys still work."
        elif EmailMfaEnrolment.objects.filter(user_id=user_pk).exists():
            tail = "Your password and email sign-in codes are now the only ways to sign in."
        else:
            tail = "Your password is now the only way to sign in."
    body = f"{template.format(when=when, tail=tail)}\n\n{_FOOTER}\n"
    try:
        html_body = notice_html(kind, when=when, tail=tail)
        send_system_email(
            to_email=email,
            subject=subject,
            text_body=body,
            html_body=html_body,
            sender_label="Security",
        )
    except Exception:
        # The change is already committed. Record the failure; do not reverse it.
        logger.exception("security notice %s failed for user %s", kind, user_pk)
        platform_record(
            action="auth.security_notice_failed",
            actor=None,
            success=False,
            metadata={"user_pk": user_pk, "notice": kind},
        )
