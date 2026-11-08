"""A webhook for a phone_number_id Akilent doesn't know is made loud, not just failed.

Prod 2026-11-08: Meta delivered messages for re-onboarded numbers (new phone_number_id)
and every one failed tenant resolution with only a warning log.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from apps.accounts.models import Account
from apps.whatsapp.models import WebhookEventLog
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.tasks import process_whatsapp_event

NOTIFY = "apps.accounts.notifications.notify_team"


def _payload(pnid, waba="waba_1", display="15550001111"):
    return {
        "entry": [
            {
                "id": waba,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "phone_number_id": pnid,
                                "display_phone_number": display,
                            },
                            "messages": [
                                {
                                    "from": "15551234567",
                                    "id": f"wamid.{pnid}",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "hi"},
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    }


class UnknownNumberWebhookTest(TestCase):
    def setUp(self):
        cache.clear()
        self.account = Account.objects.create(company_name="Co", slug="co")
        WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="known_1",
            waba_id="waba_1",
            access_token="t",
        )

    def _event(self, payload):
        return WebhookEventLog.objects.create(
            source=WebhookEventLog.Source.WHATSAPP,
            event_type="message",
            payload=payload,
        )

    def test_unknown_number_on_known_waba_alerts_owners_once(self):
        with (
            patch(NOTIFY) as notify,
            self.assertLogs("apps.whatsapp.tasks", level="ERROR") as logs,
        ):
            e1 = self._event(_payload("new_9"))
            process_whatsapp_event(e1.id)
            e2 = self._event(_payload("new_9") | {"x": 1})
            process_whatsapp_event(e2.id)

        e1.refresh_from_db()
        self.assertFalse(e1.processed)
        self.assertIn("new_9", e1.error_message)
        self.assertTrue(
            any("unknown number" in m and "new_9" in m for m in logs.output)
        )
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], self.account)
        self.assertIn("+15550001111", notify.call_args.kwargs["text"])

    def test_unknown_waba_logs_but_does_not_notify(self):
        with (
            patch(NOTIFY) as notify,
            self.assertLogs("apps.whatsapp.tasks", level="ERROR") as logs,
        ):
            process_whatsapp_event(self._event(_payload("new_9", waba="other")).id)
        notify.assert_not_called()
        self.assertTrue(any("owning_accounts=none" in m for m in logs.output))
