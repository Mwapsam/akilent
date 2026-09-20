"""Phase 0: a Meta webhook POST may batch several messages, statuses, changes and
entries. Every one must be processed - not just the first."""
import time
from unittest.mock import patch

from django.core.cache import cache

from apps.whatsapp.models import MessageLog, WebhookEventLog, WhatsAppContact
from apps.whatsapp.tasks import process_whatsapp_event
from apps.whatsapp.tests.test_auto_reply import Base
from apps.whatsapp.views import _classify_event

REPLY = "apps.whatsapp.tasks._auto_reply_during_setup"


def msg(wa_id, mid, text="Hi"):
    return {"from": wa_id, "id": mid, "timestamp": str(int(time.time())),
            "type": "text", "text": {"body": text}}


def change(messages=None, statuses=None, pnid="PNID"):
    value = {"metadata": {"phone_number_id": pnid}}
    if messages is not None:
        value["contacts"] = [{"profile": {"name": "Tester"}}]
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {"field": "messages", "value": value}


def payload(*entries):
    return {"entry": [{"changes": list(changes)} for changes in entries]}


class WebhookBatchingTest(Base):
    def setUp(self):
        super().setUp()
        # tasks caches the automation-events flag for 60s in a process-wide cache;
        # don't let this class leak a stale value into other tests.
        cache.clear()
        self.addCleanup(cache.clear)
        p = patch(REPLY)
        p.start()
        self.addCleanup(p.stop)

    def process(self, body):
        event = WebhookEventLog.objects.create(
            source="whatsapp", event_type=_classify_event(body), payload=body)
        process_whatsapp_event(event.id)
        event.refresh_from_db()
        return event

    def outbound(self, mid):
        wa, _ = WhatsAppContact.objects.get_or_create(account=self.account, phone_number="+260971234567")
        from apps.whatsapp.models import Conversation
        convo = Conversation.get_or_open(wa)
        return MessageLog.objects.create(
            account=self.account, conversation=convo, contact=wa, direction="out",
            message_id=mid, status="sent", timestamp=convo.created_at)

    def test_every_message_in_one_change_is_processed(self):
        event = self.process(payload([change(messages=[
            msg("260971111111", "wamid.A"), msg("260971111111", "wamid.B"), msg("260972222222", "wamid.C")])]))
        self.assertTrue(event.processed)
        ids = set(MessageLog.objects.filter(account=self.account).values_list("message_id", flat=True))
        self.assertEqual(ids, {"wamid.A", "wamid.B", "wamid.C"})
        self.assertEqual(WhatsAppContact.objects.filter(account=self.account).count(), 2)

    def test_every_entry_and_change_is_processed(self):
        event = self.process(payload(
            [change(messages=[msg("260971111111", "wamid.A")]),
             change(messages=[msg("260972222222", "wamid.B")])],
            [change(messages=[msg("260973333333", "wamid.C")])],
        ))
        self.assertTrue(event.processed)
        self.assertEqual(MessageLog.objects.filter(account=self.account).count(), 3)

    def test_every_status_in_a_batch_is_applied(self):
        self.outbound("wamid.O1")
        self.outbound("wamid.O2")
        self.process(payload([change(statuses=[
            {"id": "wamid.O1", "status": "delivered"}, {"id": "wamid.O2", "status": "read"}])]))
        self.assertEqual(MessageLog.objects.get(message_id="wamid.O1").status, "delivered")
        self.assertEqual(MessageLog.objects.get(message_id="wamid.O2").status, "read")

    def test_statuses_split_across_changes_are_all_applied(self):
        self.outbound("wamid.O1")
        self.outbound("wamid.O2")
        self.process(payload([change(statuses=[{"id": "wamid.O1", "status": "delivered"}]),
                              change(statuses=[{"id": "wamid.O2", "status": "delivered"}])]))
        self.assertEqual(MessageLog.objects.filter(status="delivered").count(), 2)

    def test_mixed_payload_is_classified_and_processed_for_both_kinds(self):
        self.outbound("wamid.O1")
        body = payload([change(statuses=[{"id": "wamid.O1", "status": "delivered"}]),
                        change(messages=[msg("260971111111", "wamid.A")])])
        self.assertEqual(_classify_event(body), "message")
        self.process(body)
        self.assertEqual(MessageLog.objects.get(message_id="wamid.O1").status, "delivered")
        self.assertTrue(MessageLog.objects.filter(message_id="wamid.A").exists())

    def test_one_unroutable_value_does_not_block_the_rest_of_the_batch(self):
        event = self.process(payload([
            change(messages=[msg("260971111111", "wamid.BAD")], pnid="UNKNOWN"),
            change(messages=[msg("260972222222", "wamid.GOOD")]),
        ]))
        self.assertTrue(MessageLog.objects.filter(message_id="wamid.GOOD").exists())
        self.assertFalse(MessageLog.objects.filter(message_id="wamid.BAD").exists())
        self.assertFalse(event.processed)          # still surfaced as failed, as before
        self.assertIn("UNKNOWN", event.error_message)

    def test_reprocessing_a_batch_is_idempotent(self):
        body = payload([change(messages=[msg("260971111111", "wamid.A"), msg("260971111111", "wamid.B")])])
        self.process(body)
        event = WebhookEventLog.objects.create(source="whatsapp", event_type="message", payload=body)
        process_whatsapp_event(event.id)
        self.assertEqual(MessageLog.objects.filter(account=self.account).count(), 2)

    def test_single_message_payload_still_works(self):  # regression
        self.process(payload([change(messages=[msg("260971111111", "wamid.A")])]))
        self.assertEqual(MessageLog.objects.filter(account=self.account).count(), 1)
