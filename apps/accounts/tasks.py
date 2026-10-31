import logging

from celery import shared_task

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY = 60  # seconds


@shared_task(
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_RETRY_DELAY,
    queue="celery",
)
def send_verification_email(self, user_id: int, site_name: str, link: str) -> None:
    """Render and send the account-verification email in the background.

    Keeping this off the signup request path means an unreachable mail
    server retries via Celery instead of 500ing a signup whose account
    (User/Account/Membership) has already been committed.
    """
    from django.contrib.auth.models import User

    from apps.email.services.system_templates import render_system_email

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error("send_verification_email: user %s not found", user_id)
        return

    ctx = {"user": user, "site_name": site_name, "link": link}
    subject, text_body, html_body = render_system_email(
        "accounts.verify",
        ctx,
        fallback_subject_template="accounts/verify_email_subject.txt",
        fallback_body_template="accounts/verify_email.txt",
        fallback_html_template="accounts/verify_email.html",
    )
    try:
        from apps.email.services.send import send_system_email

        send_system_email(
            to_email=user.email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            sender_label="Account",
        )
    except Exception as exc:
        logger.exception("send_verification_email: failed for user %s", user_id)
        raise self.retry(exc=exc) from exc


_CODE_MAX_RETRIES = 2
_CODE_RETRY_DELAY = 15  # seconds; a code only lives ten minutes


@shared_task(
    bind=True,
    max_retries=_CODE_MAX_RETRIES,
    default_retry_delay=_CODE_RETRY_DELAY,
    queue="celery",
)
def send_email_code(self, code_id: int) -> None:
    """Pick and email the code for a row ``email_mfa.send_code`` queued.

    A row that was replaced, used or expired meanwhile is skipped. If every try fails
    the row is deleted, so a failed send starts no cooldown and uses up no limit.
    """
    from django.utils import timezone

    from apps.accounts import email_mfa
    from apps.accounts.models import EmailLoginCode

    row = (
        EmailLoginCode.objects.select_related("user")
        .filter(pk=code_id, used_at__isnull=True, expires_at__gt=timezone.now())
        .first()
    )
    if row is None:
        return
    try:
        email_mfa.deliver_code(row)
    except Exception as exc:
        logger.exception("send_email_code: failed for user %s", row.user_id)
        if self.request.retries >= self.max_retries:
            EmailLoginCode.objects.filter(pk=code_id).delete()
            raise
        raise self.retry(exc=exc) from exc
