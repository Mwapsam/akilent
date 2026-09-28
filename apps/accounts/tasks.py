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
    subject, body = render_system_email(
        "accounts.verify",
        ctx,
        fallback_subject_template="accounts/verify_email_subject.txt",
        fallback_body_template="accounts/verify_email.txt",
    )
    try:
        from apps.email.services.send import send_system_email

        send_system_email(
            to_email=user.email,
            subject=subject,
            text_body=body,
        )
    except Exception as exc:
        logger.exception("send_verification_email: failed for user %s", user_id)
        raise self.retry(exc=exc) from exc
