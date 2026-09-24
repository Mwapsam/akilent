import pytest

try:
    from moto import mock_aws
except ImportError:
    pytest.skip(allow_module_level=True, reason="moto not installed")

import boto3
from django.test import TestCase
from apps.email.exceptions import EmailProviderError
from apps.email.providers.ses.provider import SesProvider
from apps.email.types import DomainInfo, DomainStatus
from unittest.mock import patch


class SesProviderTests(TestCase):
    def setUp(self):
        # mock_aws must stay active for the whole test, not just setUp —
        # using it as a decorator on setUp() only mocks boto3 while setUp
        # itself runs; every test method would then hit real AWS. Starting
        # it here and stopping via addCleanup keeps it active for the
        # duration of each test.
        self.mock_aws = mock_aws()
        self.mock_aws.start()
        self.addCleanup(self.mock_aws.stop)

        self.ses_client = boto3.client("sesv2", region_name="us-east-1")
        self.provider = SesProvider()

    def test_create_domain(self):
        domain = "example.com"
        info = self.provider.create_domain(domain, description="account 7")
        self.assertIsInstance(info, DomainInfo)
        self.assertEqual(info.domain, domain)
        self.assertEqual(info.status, DomainStatus.PENDING)
        self.assertIsNone(info.dkim)
        self.assertEqual(info.description, "account 7")

        # Verify identity exists in SES as a domain identity (EmailIdentity
        # is the correct sesv2 param name; the identity itself should be
        # the bare domain, not "noreply@domain" — a domain identity is
        # DNS/DKIM-verified, an address identity is link-verified).
        response = self.ses_client.get_email_identity(EmailIdentity=domain)
        self.assertEqual(response["IdentityType"], "DOMAIN")

    def test_create_domain_already_exists(self):
        domain = "example.com"
        self.provider.create_domain(domain)
        # Second call should still return a PENDING DomainInfo, not raise.
        info = self.provider.create_domain(domain)
        self.assertEqual(info.status, DomainStatus.PENDING)

    def test_unsupported_operations_raise(self):
        # SES is a sending-identity provider, not a mail server: the
        # mail-server-only methods inherit the base default that raises.
        for call in (
            lambda: self.provider.get_domain("example.com"),
            lambda: self.provider.list_domains(),
            lambda: self.provider.update_domain("example.com"),
            lambda: self.provider.set_domain_active("example.com", active=True),
            lambda: self.provider.provision_dkim("example.com"),
            lambda: self.provider.rotate_dkim("example.com", new_selector="s2"),
        ):
            with self.assertRaises(EmailProviderError):
                call()

    def test_verify_domain_success(self):
        domain = "example.com"
        self.provider.create_domain(domain)

        with patch("botocore.client.BaseClient._make_api_call") as mock_call:
            # SESv2 API returns VerificationStatus at top level
            mock_call.return_value = {
                "VerificationStatus": "SUCCESS"
            }
            result = self.provider.verify_domain(domain)
            self.assertTrue(result.success)

    def test_verify_domain_failure(self):
        domain = "example.com"
        self.provider.create_domain(domain)

        with patch("botocore.client.BaseClient._make_api_call") as mock_call:
            # SESv2 API returns VerificationStatus at top level
            mock_call.return_value = {
                "VerificationStatus": "FAILED"
            }
            result = self.provider.verify_domain(domain)
            self.assertFalse(result.success)

    def test_get_dkim(self):
        domain = "example.com"
        self.provider.create_domain(domain)

        with patch("botocore.client.BaseClient._make_api_call") as mock_call:
            # SESv2 API returns DkimAttributes.Tokens at that path
            mock_call.return_value = {
                "DkimAttributes": {"Tokens": ["token123", "token456", "token789"]}
            }
            dkim = self.provider.get_dkim(domain)
            self.assertIsNotNone(dkim)
            self.assertEqual(dkim.selector, "token123")
            self.assertEqual(dkim.public_key_txt, "token123.dkim.amazonses.com")
            self.assertEqual(dkim.record_name, "token123._domainkey.example.com")
            self.assertEqual(dkim.algorithm, "rsa-sha256")

    def test_get_dkim_records_returns_all_tokens(self):
        domain = "example.com"
        self.provider.create_domain(domain)

        with patch("botocore.client.BaseClient._make_api_call") as mock_call:
            # SESv2 API returns DkimAttributes.Tokens at that path
            mock_call.return_value = {
                "DkimAttributes": {"Tokens": ["token123", "token456", "token789"]}
            }
            records = self.provider.get_dkim_records(domain)
            self.assertEqual(len(records), 3)
            self.assertEqual(
                [r.public_key_txt for r in records],
                [
                    "token123.dkim.amazonses.com",
                    "token456.dkim.amazonses.com",
                    "token789.dkim.amazonses.com",
                ],
            )
            self.assertEqual(
                [r.record_name for r in records],
                [
                    "token123._domainkey.example.com",
                    "token456._domainkey.example.com",
                    "token789._domainkey.example.com",
                ],
            )

    def test_delete_domain(self):
        domain = "example.com"
        self.provider.create_domain(domain)
        result = self.provider.delete_domain(domain)
        self.assertTrue(result.success)


class SesFactoryResolutionTests(TestCase):
    """The "ses" aliases in the provider factory must resolve to instances."""

    def setUp(self):
        self.mock_aws = mock_aws()
        self.mock_aws.start()
        self.addCleanup(self.mock_aws.stop)

    def test_get_mail_provider_resolves_ses(self):
        from apps.email.providers import get_mail_provider
        from apps.email.providers.ses import SesProvider as _SesProvider

        with patch("apps.core.models.MailProviderSettings.load") as load:
            load.return_value.infra_backend = "ses"
            load.return_value.aws_region = "us-east-1"
            provider = get_mail_provider()
        self.assertIsInstance(provider, _SesProvider)

    def test_get_send_provider_resolves_ses(self):
        import boto3

        from apps.email.providers import get_send_provider
        from apps.email.providers.ses import SesSendProvider as _SesSendProvider

        # The SES send backend now refuses to construct without bounce/complaint
        # feedback wiring, so the resolution test has to provide it.
        boto3.client("sesv2", region_name="us-east-1").create_configuration_set(
            ConfigurationSetName="primary"
        )
        with patch("apps.core.models.MailProviderSettings.load") as load:
            load.return_value.send_backend = "ses"
            load.return_value.aws_region = "us-east-1"
            load.return_value.ses_configuration_set = "primary"
            load.return_value.ses_sns_topic_arn = "arn:aws:sns:us-east-1:1:ses-events"
            provider = get_send_provider()
        self.assertIsInstance(provider, _SesSendProvider)
