"""Phase 0: webhook handlers must only ever select the resolved tenant's records."""
from django.test import TestCase

from apps.accounts.models import Account
from apps.whatsapp.models import (
    Conversation,
    MessageLog,
    MessageTemplate,
    WebhookEventLog,
    WhatsAppContact,
)
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.tasks import process_whatsapp_event

PENDING = MessageTemplate.ApprovalStatus.PENDING
APPROVED = MessageTemplate.ApprovalStatus.APPROVED


class TwoTenants(TestCase):
    def setUp(self):
        self.a = Account.objects.create(company_name="A", slug="a")
        self.b = Account.objects.create(company_name="B", slug="b")
        WhatsAppBusinessNumber.objects.create(
            account=self.a, phone_number_id="PNID_A", waba_id="WABA_A", access_token="t", is_active=True)
        WhatsAppBusinessNumber.objects.create(
            account=self.b, phone_number_id="PNID_B", waba_id="WABA_B", access_token="t", is_active=True)

    def process(self, event_type, payload):
        event = WebhookEventLog.objects.create(source="whatsapp", event_type=event_type, payload=payload)
        process_whatsapp_event(event.id)
        event.refresh_from_db()
        return event

    def template(self, account, status=PENDING):
        return MessageTemplate.objects.create(
            account=account, name="Promo", whatsapp_template_name="promo",
            language_code="en", approval_status=status)

    def outbound(self, account, mid, status="sent"):
        wa = WhatsAppContact.objects.create(account=account, phone_number="+260971234567")
        convo = Conversation.objects.create(account=account, contact=wa)
        return MessageLog.objects.create(
            account=account, conversation=convo, contact=wa, direction="out",
            message_id=mid, status=status, timestamp=convo.created_at)


class TemplateStatusScopingTest(TwoTenants):
    def payload(self, waba_id, language="en"):
        value = {"message_template_name": "promo", "event": "APPROVED"}
        if language:
            value["message_template_language"] = language
        return {"entry": [{"id": waba_id, "changes": [
            {"field": "message_template_status_update", "value": value}]}]}

    def test_only_the_owning_waba_account_is_updated(self):
        ta, tb = self.template(self.a), self.template(self.b)
        self.process("message_template_status_update", self.payload("WABA_A"))
        ta.refresh_from_db(); tb.refresh_from_db()
        self.assertEqual(ta.approval_status, APPROVED)
        self.assertEqual(tb.approval_status, PENDING)       # same name+language, other tenant

    def test_language_fallback_stays_inside_the_tenant(self):
        ta = self.template(self.a)
        tb = self.template(self.b)
        self.process("message_template_status_update", self.payload("WABA_A", language="fr"))
        ta.refresh_from_db(); tb.refresh_from_db()
        self.assertEqual(ta.approval_status, APPROVED)      # name-only fallback within A
        self.assertEqual(tb.approval_status, PENDING)

    def test_unknown_waba_updates_nobody_and_is_surfaced(self):
        ta, tb = self.template(self.a), self.template(self.b)
        event = self.process("message_template_status_update", self.payload("WABA_UNKNOWN"))
        ta.refresh_from_db(); tb.refresh_from_db()
        self.assertEqual((ta.approval_status, tb.approval_status), (PENDING, PENDING))
        self.assertFalse(event.processed)
        self.assertIn("WABA_UNKNOWN", event.error_message)

    def test_every_change_in_the_payload_is_applied(self):
        ta = self.template(self.a)
        tb = self.template(self.b)
        self.process("message_template_status_update", {"entry": [
            self.payload("WABA_A")["entry"][0], self.payload("WABA_B")["entry"][0]]})
        ta.refresh_from_db(); tb.refresh_from_db()
        self.assertEqual((ta.approval_status, tb.approval_status), (APPROVED, APPROVED))


class MessageStatusScopingTest(TwoTenants):
    def payload(self, pnid, mid, status="delivered"):
        return {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": pnid}, "statuses": [{"id": mid, "status": status}]}}]}]}

    def test_same_message_id_in_two_accounts_updates_only_the_resolved_one(self):
        la, lb = self.outbound(self.a, "wamid.SAME"), self.outbound(self.b, "wamid.SAME")
        event = self.process("status", self.payload("PNID_B", "wamid.SAME"))
        la.refresh_from_db(); lb.refresh_from_db()
        self.assertTrue(event.processed)                    # no MultipleObjectsReturned
        self.assertEqual((la.status, lb.status), ("sent", "delivered"))

    def test_status_cannot_touch_another_tenants_message(self):
        lb = self.outbound(self.b, "wamid.B_ONLY")
        self.process("status", self.payload("PNID_A", "wamid.B_ONLY"))
        lb.refresh_from_db()
        self.assertEqual(lb.status, "sent")

    def test_status_for_unroutable_number_updates_nothing(self):
        la = self.outbound(self.a, "wamid.A1")
        self.process("status", self.payload("NOT_A_NUMBER", "wamid.A1"))
        la.refresh_from_db()
        self.assertEqual(la.status, "sent")
