"""AWS SES send provider — delivers messages via boto3 SES client.

This implementation sends through AWS Simple Email Service (SES) API,
configured to use a configuration set for tracking bounces/complaints.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from apps.email.exceptions import ConfigurationError, EmailProviderError
from apps.email.types import OutboundEmail, SendResult

from ..send_base import EmailSendProvider

if TYPE_CHECKING:
    from mypy_boto3_sesv2 import SESv2Client

logger = logging.getLogger(__name__)


class SesSendProvider(EmailSendProvider):
    """Send provider using AWS SES v2 API."""

    def __init__(self) -> None:
        from apps.core.models import MailProviderSettings

        # Read region and configuration set from MailProviderSettings; fall back to env/defaults
        region = "us-east-1"
        self.configuration_set: str | None = None
        # Retained so existing callers/tests keep working; the provider no
        # longer degrades, so this is always False.
        self.tracking_degraded = False
        sns_topic_arn = ""
        is_active_backend = False

        try:
            settings = MailProviderSettings.load()
            region = settings.aws_region or os.getenv("AWS_REGION", "us-east-1")
            self.configuration_set = settings.ses_configuration_set or None
            sns_topic_arn = settings.ses_sns_topic_arn or ""
            is_active_backend = settings.send_backend == "ses"
        except Exception:
            logger.exception("Failed to load MailProviderSettings; falling back to env/defaults")
            region = os.getenv("AWS_REGION", "us-east-1")

        # Fail closed. Without a configuration set and its SNS event
        # destinations we receive no bounce or complaint feedback, which means
        # we cannot suppress bad addresses and cannot see our own reputation
        # collapsing. Sending blind is worse than not sending.
        if is_active_backend and not (self.configuration_set and sns_topic_arn):
            raise ConfigurationError(
                "SES is the active send backend but bounce/complaint feedback is "
                "not configured: both MailProviderSettings.ses_configuration_set "
                "and .ses_sns_topic_arn are required. Run `manage.py "
                "setup_ses_reputation_monitoring` to provision them."
            )

        self.client: SESv2Client = boto3.client("sesv2", region_name=region)

        if self.configuration_set:
            self._ensure_configuration_set()


    def _ensure_configuration_set(self) -> None:
        """Confirm the configured set exists, or refuse to send.

        This used to degrade to sending without a configuration set. That left
        the worker delivering mail with no bounce/complaint feedback at all,
        which is precisely the blind spot SES production-access review asks
        about — so it now fails closed instead.
        """
        try:
            self.client.get_configuration_set(ConfigurationSetName=self.configuration_set)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "NotFoundException":
                raise ConfigurationError(
                    f"SES configuration set {self.configuration_set!r} does not "
                    "exist, so bounce/complaint tracking would be off. Create it "
                    "and its SNS event destinations (`manage.py "
                    "setup_ses_reputation_monitoring`), then retry."
                ) from exc
            # Any other error means we cannot prove tracking is attached.
            raise ConfigurationError(
                f"Could not verify SES configuration set "
                f"{self.configuration_set!r}: {exc}"
            ) from exc

    def send(self, message: OutboundEmail) -> SendResult:
        if not message.html_body and not message.text_body:
            raise EmailProviderError(
                f"Cannot send message to={message.to_email!r}: "
                "neither html_body nor text_body is set"
            )

        from apps.email.services.rate_limiter import get_ses_rate_limiter

        rate_limiter = get_ses_rate_limiter()
        waited = rate_limiter.wait_for(1.0)
        if waited > 0.01:
            logger.debug("SES send: waited %.3f sec for rate limit token", waited)

        try:
            params = {
                "FromEmailAddress": message.from_email,
                "Destination": {
                    "ToAddresses": [message.to_email],
                },
                "Content": {
                    "Simple": {
                        "Subject": {
                            "Data": message.subject,
                            "Charset": "UTF-8",
                        },
                    }
                },
            }

            # Attachments require a raw MIME send — SES has no "Simple" slot for them.
            if message.attachments:
                from apps.email.services.mime import build_mime

                params["Content"] = {"Raw": {"Data": build_mime(message)}}
                if self.configuration_set:
                    params["ConfigurationSetName"] = self.configuration_set
                response = self.client.send_email(**params)
                message_id = response.get("MessageId")
                if not message_id:
                    raise EmailProviderError("SES returned no MessageId")
                logger.info(
                    "SES raw send successful: to=%s, message_id=%s, attachments=%d",
                    message.to_email, message_id, len(message.attachments),
                )
                return SendResult(success=True, provider_message_id=message_id)

            # Add body parts (prefer HTML, fallback to text-only)
            if message.html_body:
                params["Content"]["Simple"]["Body"] = {
                    "Html": {
                        "Data": message.html_body,
                        "Charset": "UTF-8",
                    },
                }
                if message.text_body:
                    params["Content"]["Simple"]["Body"]["Text"] = {
                        "Data": message.text_body,
                        "Charset": "UTF-8",
                    }
            elif message.text_body:
                params["Content"]["Simple"]["Body"] = {
                    "Text": {
                        "Data": message.text_body,
                        "Charset": "UTF-8",
                    }
                }

            # Add custom headers (e.g., List-Unsubscribe for bulk senders)
            if message.headers:
                params["Content"]["Simple"]["Headers"] = [
                    {"Name": k, "Value": v} for k, v in message.headers.items()
                ]

            # Add configuration set if configured
            if self.configuration_set:
                params["ConfigurationSetName"] = self.configuration_set

            response = self.client.send_email(**params)
            message_id = response.get("MessageId")

            if not message_id:
                raise EmailProviderError("SES returned no MessageId")

            logger.info(
                "SES send successful: to=%s, message_id=%s",
                message.to_email,
                message_id,
            )
            return SendResult(success=True, provider_message_id=message_id)

        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            error_msg = exc.response.get("Error", {}).get("Message", str(exc))

            retryable_codes = {"TooManyRequestsException", "LimitExceededException"}
            if error_code in retryable_codes:
                logger.warning(
                    "SES send retryable error: %s - %s (to=%s)",
                    error_code,
                    error_msg,
                    message.to_email,
                )
            else:
                logger.error(
                    "SES send failed: %s - %s (to=%s)",
                    error_code,
                    error_msg,
                    message.to_email,
                )

            raise EmailProviderError(f"{error_code}: {error_msg}") from exc

        except Exception as exc:
            logger.error(
                "SES send unexpected error: %s (to=%s)",
                str(exc),
                message.to_email,
                exc_info=True,
            )
            raise EmailProviderError(str(exc)) from exc