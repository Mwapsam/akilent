import pytest
import json
import requests
from unittest.mock import patch, MagicMock
from django.test import TestCase, RequestFactory
from apps.email.ses_webhooks import ses_sns_webhook
from apps.email.models import SuppressionListEntry
from apps.email.models import EmailMessage
from apps.accounts.models import Account

class SesWebhookTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.account = Account.objects.create(company_name="Test Account")
        self.email_msg = EmailMessage.objects.create(
            account=self.account,
            provider_message_id="msg-123",
            subject="Test Subject",
            from_email="test@example.com",
            to_email="recipient@example.com"
        )

    def test_ses_sns_webhook_subscription_confirmation(self):
        payload = {
            "Type": "SubscriptionConfirmation",
            "TopicArn": "arn:aws:sns:us-east-1:123456789:test-topic",
            "SubscribeURL": "http://example.com/confirm",
            "Signature": "valid-sig",
            "SigningCertUrl": "http://example.com/cert"
        }

        with patch("apps.email.ses_webhooks._verify_sns_signature", return_value=True):
            with patch("apps.email.ses_webhooks._get_sns_topic_arn_if_allowed", return_value="arn:aws:sns:us-east-1:123456789:test-topic"):
                with patch("apps.email.ses_webhooks._is_valid_sns_url", return_value=True):
                    with patch("requests.get") as mock_get:
                        request = self.rf.post("/webhooks/ses/", data=json.dumps(payload), content_type="application/json")
                        response = ses_sns_webhook(request)

                        self.assertEqual(response.status_code, 200)
                        mock_get.assert_called_once_with("http://example.com/confirm", timeout=5, allow_redirects=False)

    def test_ses_sns_webhook_invalid_signature(self):
        payload = {
            "Type": "Notification",
            "Signature": "invalid-sig",
            "SigningCertUrl": "http://example.com/cert"
        }

        with patch("apps.email.ses_webhooks._verify_sns_signature", return_value=False):
            request = self.rf.post("/webhooks/ses/", data=json.dumps(payload), content_type="application/json")
            response = ses_sns_webhook(request)

            self.assertEqual(response.status_code, 403)

    def test_ses_sns_webhook_bounce(self):
        payload = {
            "Type": "Notification",
            "TopicArn": "arn:aws:sns:us-east-1:123456789:test-topic",
            "Message": json.dumps({
                "eventType": "Bounce",
                "bounce": {
                    "bounceType": "Permanent",
                    "bouncedRecipients": [{"emailAddress": "recipient@example.com"}]
                },
                "mail": {"messageId": "msg-123"}
            }),
            "Signature": "valid-sig",
            "SigningCertUrl": "http://example.com/cert"
        }

        with patch("apps.email.ses_webhooks._verify_sns_signature", return_value=True):
            with patch("apps.email.ses_webhooks._get_sns_topic_arn_if_allowed", return_value="arn:aws:sns:us-east-1:123456789:test-topic"):
                request = self.rf.post("/webhooks/ses/", data=json.dumps(payload), content_type="application/json")
                response = ses_sns_webhook(request)

                self.assertEqual(response.status_code, 200)
                suppression = SuppressionListEntry.objects.filter(email="recipient@example.com").first()
                self.assertIsNotNone(suppression)
                self.assertEqual(suppression.reason, SuppressionListEntry.Reason.BOUNCE)

    def test_ses_sns_webhook_complaint(self):
        payload = {
            "Type": "Notification",
            "TopicArn": "arn:aws:sns:us-east-1:123456789:test-topic",
            "Message": json.dumps({
                "eventType": "Complaint",
                "complaint": {
                    "complainedRecipients": [{"emailAddress": "recipient@example.com"}]
                },
                "mail": {"messageId": "msg-123"}
            }),
            "Signature": "valid-sig",
            "SigningCertUrl": "http://example.com/cert"
        }

        with patch("apps.email.ses_webhooks._verify_sns_signature", return_value=True):
            with patch("apps.email.ses_webhooks._get_sns_topic_arn_if_allowed", return_value="arn:aws:sns:us-east-1:123456789:test-topic"):
                request = self.rf.post("/webhooks/ses/", data=json.dumps(payload), content_type="application/json")
                response = ses_sns_webhook(request)

                self.assertEqual(response.status_code, 200)
                suppression = SuppressionListEntry.objects.filter(email="recipient@example.com").first()
                self.assertIsNotNone(suppression)
                self.assertEqual(suppression.reason, SuppressionListEntry.Reason.COMPLAINT)


class OrphanedSesEventTests(TestCase):
    """Bounce/complaint events we can't attribute to a tenant must still suppress.

    These used to be dropped with a log line, which left a known-bad address
    mailable by every account and kept costing us bounce rate against our SES
    account.
    """

    TOPIC = "arn:aws:sns:us-east-1:123456789:test-topic"

    def setUp(self):
        self.rf = RequestFactory()
        self.account = Account.objects.create(company_name="Test Account")

    def _post(self, message: dict):
        payload = {
            "Type": "Notification",
            "TopicArn": self.TOPIC,
            "Message": json.dumps(message),
            "Signature": "valid-sig",
            "SigningCertUrl": "http://example.com/cert",
        }
        with patch("apps.email.ses_webhooks._verify_sns_signature", return_value=True):
            with patch(
                "apps.email.ses_webhooks._get_sns_topic_arn_if_allowed",
                return_value=self.TOPIC,
            ):
                request = self.rf.post(
                    "/webhooks/ses/",
                    data=json.dumps(payload),
                    content_type="application/json",
                )
                return ses_sns_webhook(request)

    def test_unattributable_hard_bounce_suppresses_platform_wide(self):
        from apps.email.models import GlobalSuppression

        response = self._post({
            "eventType": "Bounce",
            "bounce": {
                "bounceType": "Permanent",
                "bouncedRecipients": [{"emailAddress": "ghost@example.com"}],
            },
            # Neither the message id nor the sender domain resolves.
            "mail": {"messageId": "unknown-msg", "source": "who@nowhere.invalid"},
        })

        self.assertEqual(response.status_code, 200)
        entry = GlobalSuppression.objects.get(email="ghost@example.com")
        self.assertEqual(entry.reason, GlobalSuppression.Reason.BOUNCE)
        self.assertEqual(entry.source, "orphan_sns")

        # ...and it now blocks sends from an unrelated account.
        from apps.email.services.suppression import is_suppressed

        other = Account.objects.create(company_name="Unrelated Tenant")
        self.assertTrue(is_suppressed(other, "ghost@example.com"))

    def test_unattributable_complaint_suppresses_platform_wide(self):
        from apps.email.models import GlobalSuppression
        from apps.email.services.suppression import is_suppressed

        response = self._post({
            "eventType": "Complaint",
            "complaint": {
                "complainedRecipients": [{"emailAddress": "angry@example.com"}],
            },
            "mail": {"messageId": "unknown-msg-2", "source": "who@nowhere.invalid"},
        })

        self.assertEqual(response.status_code, 200)
        entry = GlobalSuppression.objects.get(email="angry@example.com")
        self.assertEqual(entry.reason, GlobalSuppression.Reason.COMPLAINT)

        other = Account.objects.create(company_name="Unrelated Tenant 2")
        self.assertTrue(is_suppressed(other, "angry@example.com"))

    def test_unattributable_soft_bounce_does_not_block_everyone(self):
        """A transient failure for one sender is no reason to block globally."""
        from apps.email.models import GlobalSuppression

        response = self._post({
            "eventType": "Bounce",
            "bounce": {
                "bounceType": "Transient",
                "bouncedRecipients": [{"emailAddress": "busy@example.com"}],
            },
            "mail": {"messageId": "unknown-msg-3", "source": "who@nowhere.invalid"},
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            GlobalSuppression.objects.filter(email="busy@example.com").exists()
        )

    def test_attributed_hard_bounce_also_writes_through_platform_wide(self):
        """A bounce is a property of the address, not of the tenant that hit it."""
        from apps.email.models import GlobalSuppression
        from apps.email.services.suppression import is_suppressed

        EmailMessage.objects.create(
            account=self.account,
            provider_message_id="msg-attributed",
            subject="s", from_email="test@example.com", to_email="dead@example.com",
        )
        self._post({
            "eventType": "Bounce",
            "bounce": {
                "bounceType": "Permanent",
                "bouncedRecipients": [{"emailAddress": "dead@example.com"}],
            },
            "mail": {"messageId": "msg-attributed"},
        })

        self.assertTrue(
            GlobalSuppression.objects.filter(email="dead@example.com").exists()
        )
        other = Account.objects.create(company_name="Unrelated Tenant 3")
        self.assertTrue(is_suppressed(other, "dead@example.com"))

    def test_unsubscribe_stays_tenant_scoped(self):
        """One sender's opt-out must not block a different sender's mail."""
        from apps.email.models import GlobalSuppression
        from apps.email.services.suppression import is_suppressed, record_event

        record_event(account=self.account, email="quiet@example.com", reason="unsubscribe")

        self.assertFalse(
            GlobalSuppression.objects.filter(email="quiet@example.com").exists()
        )
        self.assertTrue(is_suppressed(self.account, "quiet@example.com"))
        other = Account.objects.create(company_name="Unrelated Tenant 4")
        self.assertFalse(is_suppressed(other, "quiet@example.com"))
