"""Read-only trace: how far did each inbound WhatsApp message get through the pipeline?"""
import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.whatsapp.models import Conversation as WaConversation
from apps.whatsapp.models import MessageLog, WebhookEventLog, WhatsAppContact

NOW = timezone.now()


class TraceInboundMessagesTest(TestCase):
    def setUp(self):
        self.a = Account.objects.create(company_name="A", slug="a")
        self.b = Account.objects.create(company_name="B", slug="b")
        self._n = 0

    def inbound(self, account, *, project=False, event=None, mid=None):
        """event: None | dict(processed=, attempts=, error=)"""
        self._n += 1
        phone = f"+26097{3000000 + self._n}"
        contact = Contact.objects.create(account=account, phone=phone)
        wa = WhatsAppContact.objects.create(account=account, phone_number=phone, contact=contact)
        wa_convo = WaConversation.get_or_open(wa)
        mid = mid or f"wamid.TRACE{self._n}"
        log = MessageLog.objects.create(
            account=account, conversation=wa_convo, contact=wa, direction="in", message_id=mid,
            message_type="text", content="SECRET BODY", status="delivered", timestamp=NOW)
        if project:
            generic = Conversation.get_or_create_for_whatsapp(wa_convo)
            Message.objects.create(account=account, conversation=generic, direction="inbound",
                                   body="SECRET BODY", timestamp=NOW, whatsapp_message=log)
        if event is not None:
            WebhookEventLog.objects.create(
                source="whatsapp", event_type="message", processed=event.get("processed", True),
                attempts=event.get("attempts", 0), error_message=event.get("error"),
                payload={"entry": [{"changes": [{"value": {"messages": [{"id": mid}]}}]}]})
        return log

    def run_cmd(self, *args):
        out = StringIO()
        call_command("trace_inbound_messages", *args, stdout=out)
        return out.getvalue()

    def verdicts(self, account="a"):
        data = json.loads(self.run_cmd("--account", account, "--json"))
        return {m["message_id"]: m["verdict"] for m in data["messages"]}, data

    def test_each_message_gets_the_stage_it_reached(self):
        self.inbound(self.a, project=True, event={"processed": True}, mid="wamid.OK")
        self.inbound(self.a, event={"processed": True}, mid="wamid.NOPROJ")
        self.inbound(self.a, event={"processed": False, "attempts": 3}, mid="wamid.UNPROC")
        self.inbound(self.a, event={"processed": False, "attempts": 2, "error": "boom"}, mid="wamid.ERR")
        self.inbound(self.a, mid="wamid.NOEVENT")
        verdicts, data = self.verdicts()
        self.assertEqual(verdicts, {
            "wamid.OK": "projected",
            "wamid.NOPROJ": "logged_not_projected",
            "wamid.UNPROC": "event_not_processed",
            "wamid.ERR": "event_error",
            "wamid.NOEVENT": "logged_without_stored_event",
        })
        self.assertEqual(data["summary"]["projected"], 1)
        self.assertEqual(sum(data["summary"].values()), 5)

    def test_projected_message_reports_visible_conversation(self):
        self.inbound(self.a, project=True, event={}, mid="wamid.OK")
        row = json.loads(self.run_cmd("--account", "a", "--json"))["messages"][0]
        self.assertTrue(row["projected"] and row["conversation_id"].startswith("conv_"))
        self.assertTrue(row["event_stored"])

    def test_is_account_scoped(self):
        self.inbound(self.b, mid="wamid.B_ONLY")
        verdicts, _ = self.verdicts("a")
        self.assertEqual(verdicts, {})

    def test_never_prints_content_or_phone_numbers_and_writes_nothing(self):
        self.inbound(self.a, event={"processed": False, "error": "boom"})
        before = (MessageLog.objects.count(), Message.objects.count(), WebhookEventLog.objects.count())
        with CaptureQueriesContext(connection) as ctx:
            text = self.run_cmd("--account", "a")
            js = self.run_cmd("--account", "a", "--json")
        for out in (text, js):
            self.assertNotIn("SECRET BODY", out)
            self.assertNotIn("+26097", out)
        for q in ctx.captured_queries:
            self.assertNotRegex(q["sql"].lstrip().upper(), r"^(INSERT|UPDATE|DELETE|SAVEPOINT)")
        self.assertEqual(before, (MessageLog.objects.count(), Message.objects.count(),
                                  WebhookEventLog.objects.count()))

    def test_lists_message_events_that_never_produced_a_message_log(self):
        WebhookEventLog.objects.create(
            source="whatsapp", event_type="message", processed=False, attempts=5,
            error_message="No active WhatsAppBusinessNumber found for phone_number_id=X",
            payload={"entry": []})
        WebhookEventLog.objects.create(source="whatsapp", event_type="message", processed=True,
                                       payload={"entry": []})
        data = json.loads(self.run_cmd("--account", "a", "--json"))
        orphans = data["unprocessed_message_events"]
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["attempts"], 5)
        self.assertIn("No active WhatsAppBusinessNumber", orphans[0]["error"])

    def test_report_and_validation(self):
        self.inbound(self.a, project=True, event={})
        report = self.run_cmd("--account", "a")
        self.assertIn("projected", report)
        with self.assertRaises(CommandError):
            self.run_cmd("--account", "nope")
