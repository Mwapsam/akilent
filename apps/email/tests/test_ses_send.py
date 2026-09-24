"""Unit tests for the AWS SES send provider (SesSendProvider)."""
import pytest

try:
    from moto import mock_aws
except ImportError:
    pytest.skip(allow_module_level=True, reason="moto not installed")

from unittest.mock import MagicMock, patch

import boto3
from botocore.exceptions import ClientError
from django.test import TestCase

from apps.email.exceptions import ConfigurationError, EmailProviderError
from apps.email.providers.ses.send import SesSendProvider
from apps.email.types import OutboundEmail


def _settings(**over):
    base = dict(
        aws_region="us-east-1",
        ses_configuration_set="",
        ses_sns_topic_arn="",
        ses_send_rate_limit=14,
        send_backend="smtp",
    )
    base.update(over)
    m = MagicMock()
    for k, v in base.items():
        setattr(m, k, v)
    return m


class SesSendProviderTests(TestCase):
    def setUp(self):
        self.mock_aws = mock_aws()
        self.mock_aws.start()
        self.addCleanup(self.mock_aws.stop)
        # Verify the sender identity so moto's send_email accepts it.
        boto3.client("sesv2", region_name="us-east-1").create_email_identity(
            EmailIdentity="acme.com"
        )

    def _provider(self, **settings_over):
        with patch("apps.core.models.MailProviderSettings.load",
                   return_value=_settings(**settings_over)):
            return SesSendProvider()

    def _msg(self, **over):
        base = dict(
            from_email="no-reply@acme.com",
            to_email="rcpt@example.com",
            subject="Hi",
            text_body="plain",
            html_body="<p>rich</p>",
        )
        base.update(over)
        return OutboundEmail(**base)

    def test_send_html_and_text(self):
        provider = self._provider()
        with patch.object(provider.client, "send_email",
                          wraps=provider.client.send_email) as spy:
            res = provider.send(self._msg())
        self.assertTrue(res.success)
        self.assertTrue(res.provider_message_id)
        body = spy.call_args.kwargs["Content"]["Simple"]["Body"]
        self.assertIn("Html", body)
        self.assertIn("Text", body)

    def test_send_text_only(self):
        provider = self._provider()
        with patch.object(provider.client, "send_email",
                          wraps=provider.client.send_email) as spy:
            provider.send(self._msg(html_body=""))
        body = spy.call_args.kwargs["Content"]["Simple"]["Body"]
        self.assertIn("Text", body)
        self.assertNotIn("Html", body)

    def test_send_requires_a_body(self):
        provider = self._provider()
        with self.assertRaises(EmailProviderError):
            provider.send(self._msg(text_body="", html_body=""))

    def test_custom_headers_passed_through(self):
        provider = self._provider()
        headers = {
            "List-Unsubscribe": "<mailto:u@acme.com>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
        with patch.object(provider.client, "send_email",
                          wraps=provider.client.send_email) as spy:
            provider.send(self._msg(headers=headers))
        sent = {
            h["Name"]: h["Value"]
            for h in spy.call_args.kwargs["Content"]["Simple"]["Headers"]
        }
        self.assertEqual(sent, headers)

    def test_configuration_set_attached_when_present(self):
        client = boto3.client("sesv2", region_name="us-east-1")
        client.create_configuration_set(ConfigurationSetName="primary")
        provider = self._provider(ses_configuration_set="primary")
        self.assertEqual(provider.configuration_set, "primary")
        with patch.object(provider.client, "send_email",
                          wraps=provider.client.send_email) as spy:
            provider.send(self._msg())
        self.assertEqual(spy.call_args.kwargs["ConfigurationSetName"], "primary")

    def test_missing_configuration_set_fails_closed(self):
        """A config set that doesn't exist means no bounce/complaint feedback.

        Sending anyway would leave us delivering blind, so construction raises
        rather than degrading.
        """
        with self.assertRaises(ConfigurationError):
            self._provider(ses_configuration_set="does-not-exist")

    def test_ses_backend_requires_feedback_wiring(self):
        client = boto3.client("sesv2", region_name="us-east-1")
        client.create_configuration_set(ConfigurationSetName="primary")

        # Config set but no SNS topic: the events have nowhere to go.
        with self.assertRaises(ConfigurationError):
            self._provider(
                send_backend="ses",
                ses_configuration_set="primary",
                ses_sns_topic_arn="",
            )

        # SNS topic but no config set: nothing publishes to it.
        with self.assertRaises(ConfigurationError):
            self._provider(
                send_backend="ses",
                ses_configuration_set="",
                ses_sns_topic_arn="arn:aws:sns:us-east-1:1:ses-events",
            )

        # Both present: constructs fine.
        provider = self._provider(
            send_backend="ses",
            ses_configuration_set="primary",
            ses_sns_topic_arn="arn:aws:sns:us-east-1:1:ses-events",
        )
        self.assertEqual(provider.configuration_set, "primary")

    def test_throttling_raises_provider_error_for_retry(self):
        provider = self._provider()
        throttle = ClientError(
            {"Error": {"Code": "TooManyRequestsException", "Message": "slow down"}},
            "SendEmail",
        )
        with patch.object(provider.client, "send_email", side_effect=throttle):
            with self.assertRaises(EmailProviderError):
                provider.send(self._msg())
