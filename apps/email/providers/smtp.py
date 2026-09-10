"""SMTP send provider — wraps apps.email.services.send.smtp_send unchanged.

This is the default (and today, only) EmailSendProvider implementation: it
delivers through the self-hosted SMTP relay exactly as before. Swapping in a
SendGrid/Mailgun/SES/Resend provider later means adding a sibling module and
pointing EMAIL_SEND_PROVIDER_BACKEND at it — this class and its call sites
don't change.
"""
from __future__ import annotations

from apps.email.exceptions import EmailProviderError
from apps.email.types import OutboundEmail, SendResult

from .send_base import EmailSendProvider


class SmtpSendProvider(EmailSendProvider):
    def send(self, message: OutboundEmail) -> SendResult:
        from apps.email.services.send import smtp_send

        try:
            message_id = smtp_send(
                from_email=message.from_email,
                to_email=message.to_email,
                subject=message.subject,
                text_body=message.text_body,
                html_body=message.html_body,
                headers=message.headers or None,
                attachments=message.attachments or None,
            )
        except Exception as exc:
            raise EmailProviderError(str(exc)) from exc
        return SendResult(success=True, provider_message_id=message_id)
