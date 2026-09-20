"""Phase 5: Meta template sync + live status webhook."""
from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import Account
from apps.whatsapp.models import MessageTemplate, WebhookEventLog
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.tasks import process_whatsapp_event, sync_templates


class _TemplateProvider:
    def __init__(self, templates):
        self._templates = templates

    def list_templates(self, waba_id):
        return self._templates


class SyncTemplatesTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")
        WhatsAppBusinessNumber.objects.create(
            account=self.account, phone_number_id="PNID",
            waba_id="WABA1", access_token="tok", is_active=True,
        )

    def test_sync_upserts_status_and_category(self):
        provider = _TemplateProvider([
            {"name": "order_update", "language": "en", "category": "UTILITY",
             "status": "APPROVED"},
            {"name": "promo", "language": "en", "category": "MARKETING",
             "status": "REJECTED"},
        ])
        with patch(
            "apps.whatsapp.providers.get_whatsapp_provider", return_value=provider
        ):
            result = sync_templates()

        self.assertEqual(result["synced"], 2)
        t1 = MessageTemplate.objects.get(whatsapp_template_name="order_update")
        self.assertEqual(t1.approval_status, MessageTemplate.ApprovalStatus.APPROVED)
        self.assertEqual(t1.category, MessageTemplate.Category.UTILITY)
        t2 = MessageTemplate.objects.get(whatsapp_template_name="promo")
        self.assertEqual(t2.approval_status, MessageTemplate.ApprovalStatus.REJECTED)

        # Re-sync updates status in place, no duplicate row.
        provider._templates[1]["status"] = "APPROVED"
        with patch(
            "apps.whatsapp.providers.get_whatsapp_provider", return_value=provider
        ):
            sync_templates()
        self.assertEqual(
            MessageTemplate.objects.filter(whatsapp_template_name="promo").count(), 1
        )
        t2.refresh_from_db()
        self.assertEqual(t2.approval_status, MessageTemplate.ApprovalStatus.APPROVED)

    def test_status_webhook_updates_template_live(self):
        tpl = MessageTemplate.objects.create(
            account=self.account, name="Promo", whatsapp_template_name="promo",
            language_code="en", approval_status=MessageTemplate.ApprovalStatus.PENDING,
        )
        event = WebhookEventLog.objects.create(
            source=WebhookEventLog.Source.WHATSAPP,
            event_type="message_template_status_update",
            payload={
                "entry": [
                    {
                        "id": "WABA1",  # Meta sends the WABA id; it scopes the update to the tenant
                        "changes": [
                            {
                                "field": "message_template_status_update",
                                "value": {
                                    "message_template_name": "promo",
                                    "message_template_language": "en",
                                    "event": "APPROVED",
                                },
                            }
                        ]
                    }
                ]
            },
        )
        process_whatsapp_event(event.id)

        tpl.refresh_from_db()
        self.assertEqual(tpl.approval_status, MessageTemplate.ApprovalStatus.APPROVED)
        event.refresh_from_db()
        self.assertTrue(event.processed)
