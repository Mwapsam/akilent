"""Transactional email sending and self-hosted open/click tracking.

Tracking is fully Django-native — no external API call. Each outgoing link is
rewritten to a Django redirect endpoint; an open-pixel is injected before
</body>. Both resolve through EmailTrackingToken rows minted here and consumed
by the tracking views (apps.email.views.tracking_open / tracking_click).
"""
from __future__ import annotations

import logging
import re
import secrets

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection

logger = logging.getLogger(__name__)

_HREF_RE = re.compile(r'href="(https?://[^"]+)"', re.IGNORECASE)
_MAX_TRACKED_LINKS = 25


def _tracking_base() -> str:
    base_domain = getattr(settings, "BASE_DOMAIN", "") or settings.ALLOWED_HOSTS[0]
    scheme = "http" if settings.DEBUG else "https"
    return f"{scheme}://{base_domain}"


def _mint_token(message, recipient: str, url: str = "") -> str:
    from apps.email.models import EmailTrackingToken

    token = secrets.token_urlsafe(32)
    EmailTrackingToken.objects.create(
        token=token,
        message=message,
        recipient=recipient,
        url=url,
    )
    return token


def apply_tracking(html_body: str, message, recipient: str, domain: str) -> str:
    """Rewrite outgoing HTML to inject open-pixel and click-tracking URLs.

    ``message`` must be an EmailMessage ORM instance (not a raw ID).
    Best-effort: returns the original HTML unchanged on any failure so tracking
    never blocks delivery.
    """
    if not html_body:
        return html_body
    try:
        base = _tracking_base()
        open_token = _mint_token(message, recipient, url="")
        open_url = f"{base}/email/t/open/{open_token}/"
        seen: dict[str, str] = {}
        count = 0

        def _rewrite(match: re.Match) -> str:
            nonlocal count
            url = match.group(1)
            if count >= _MAX_TRACKED_LINKS:
                return match.group(0)
            if url not in seen:
                click_token = _mint_token(message, recipient, url=url)
                seen[url] = f"{base}/email/t/click/{click_token}/"
                count += 1
            return f'href="{seen[url]}"'

        html = _HREF_RE.sub(_rewrite, html_body)
        pixel = (
            f'<img src="{open_url}" width="1" height="1" alt="" style="display:none">'
        )
        idx = html.lower().rfind("</body>")
        return html[:idx] + pixel + html[idx:] if idx != -1 else html + pixel
    except Exception:
        logger.exception("apply_tracking: unexpected error; sending untracked")
        return html_body


def send_system_email(
    to_email: str,
    subject: str,
    text_body: str = "",
    html_body: str = "",
) -> None:
    """Send a system email (password reset, verification, etc.) with suppression checks.

    Emails to suppressed addresses are silently dropped (logged at warning level).
    Uses the configured send provider (SES or SMTP).
    Does not require domain verification or an Account — for platform system emails only.

    Raises on non-suppression errors (caller should log/retry).
    """
    from apps.email.providers import get_send_provider
    from apps.email.types import OutboundEmail
    from apps.email.services.suppression import is_suppressed_globally
    from apps.email.services.validation import validate_recipient

    # Check if recipient is suppressed (global check, not account-specific)
    if is_suppressed_globally(to_email):
        logger.warning("Suppressing system email to %s (suppression list)", to_email)
        return

    # Validate recipient address (syntax + MX record)
    if not validate_recipient(to_email):
        logger.warning("Skipping system email to %s (validation failed)", to_email)
        return

    from_email = settings.DEFAULT_FROM_EMAIL

    try:
        result = get_send_provider().send(OutboundEmail(
            from_email=from_email,
            to_email=to_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        ))
        logger.debug("Sent system email to %s via %s", to_email, get_send_provider().__class__.__name__)
    except Exception as exc:
        logger.error("Failed to send system email to %s: %s", to_email, exc)
        raise


def smtp_send(
    from_email: str,
    to_email: str,
    subject: str,
    text_body: str = "",
    html_body: str = "",
    headers: dict[str, str] | None = None,
    attachments=None,
) -> str:
    """Send one email through the configured SMTP relay (Stalwart submission port).

    Returns the local Message-ID. Raises on failure (caller logs/marks failed).
    ``headers`` are added verbatim (e.g. List-Unsubscribe / List-Unsubscribe-Post).
    """
    connection = get_connection(
        host=settings.EMAIL_HOST,
        port=settings.EMAIL_PORT,
        username=settings.EMAIL_HOST_USER,
        password=settings.EMAIL_HOST_PASSWORD,
        use_tls=settings.EMAIL_USE_TLS,
    )
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body or " ",
        from_email=from_email,
        to=[to_email],
        connection=connection,
        headers=headers or None,
    )
    if html_body:
        msg.attach_alternative(html_body, "text/html")
    for att in attachments or ():
        msg.attach(att.filename, att.content, att.content_type)
    msg.send(fail_silently=False)
    return msg.extra_headers.get("Message-ID", "")
