"""Meta's quality and account webhooks reach the business's owners."""

from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import Account
from apps.whatsapp.models import WebhookEventLog
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.tasks import process_whatsapp_event


def _event(field, value, waba="WABA1"):
    return WebhookEventLog.objects.create(
        source=WebhookEventLog.Source.WHATSAPP,
        event_type=field,
        payload={
            "entry": [{"id": waba, "changes": [{"field": field, "value": value}]}]
        },
    )


class AccountHealthTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Health Co", slug="health")
        WhatsAppBusinessNumber.objects.create(
            account=self.account, phone_number_id="PN1", waba_id="WABA1", is_active=True
        )

    def _process(self, event):
        with patch("apps.accounts.notifications.notify_team", return_value=1) as notify:
            process_whatsapp_event(event.id)
        event.refresh_from_db()
        self.assertTrue(event.processed)
        return notify

    def test_quality_downgrade_emails_the_owners(self):
        notify = self._process(
            _event(
                "phone_number_quality_update",
                {
                    "display_phone_number": "+260 97 000",
                    "event": "DOWNGRADE",
                    "current_limit": "TIER_250",
                },
            )
        )
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        self.assertEqual(kwargs["to"], "owners")
        self.assertIn("+260 97 000", kwargs["text"])
        self.assertIn("250", kwargs["text"])

    def test_account_restriction_emails_the_owners(self):
        notify = self._process(
            _event("account_update", {"event": "ACCOUNT_RESTRICTION"})
        )
        notify.assert_called_once()

    def test_good_news_is_only_logged(self):
        notify = self._process(
            _event(
                "phone_number_quality_update",
                {"display_phone_number": "+260 97 000", "event": "UPGRADE"},
            )
        )
        notify.assert_not_called()

    def test_another_businesss_waba_is_not_alerted(self):
        with patch("apps.accounts.notifications.notify_team") as notify:
            process_whatsapp_event(
                _event("account_update", {"event": "DISABLED_UPDATE"}, waba="OTHER").id
            )
        notify.assert_not_called()
