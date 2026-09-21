"""Phase 1 instrumentation: is the conversation spine trustworthy? (read-only report)"""
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
from apps.conversations.quality import spine_quality
from apps.whatsapp.models import Conversation as WaConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact

NOW = timezone.now()


def spine(account, *messages, phone):
    """messages: (direction, minutes_ago, status[, created_minutes_ago])"""
    contact = Contact.objects.create(account=account, phone=phone)
    c = Conversation.objects.create(account=account, contact=contact, channel="whatsapp")
    for m in messages:
        direction, ago, status = m[:3]
        msg = Message.objects.create(
            account=account, conversation=c, direction=direction, body="x",
            timestamp=NOW - timedelta(minutes=ago), status=status)
        if len(m) == 4:
            Message.objects.filter(pk=msg.pk).update(created_at=NOW - timedelta(minutes=m[3]))
    return c


class SpineQualityTest(TestCase):
    def setUp(self):
        self.a = Account.objects.create(company_name="A", slug="a")
        self.b = Account.objects.create(company_name="B", slug="b")

    def test_counts_indeterminate_conversations(self):
        spine(self.a, phone="+260971000001")                                  # no messages
        spine(self.a, ("outbound", 5, "queued"), phone="+260971000002")       # nothing actually sent
        spine(self.a, ("inbound", 5, "delivered"), phone="+260971000003")     # fine
        self.assertEqual(spine_quality(self.a, now=NOW)["conversations_with_indeterminate_state"], 2)

    def test_counts_invalid_ordering_only_beyond_tolerance(self):
        # recorded last, but stamped 30 minutes before an earlier-recorded message: who-spoke-last is unreliable
        spine(self.a, ("inbound", 5, "delivered", 20), ("outbound", 35, "sent", 1), phone="+260971000004")
        # a small delay in a webhook is normal
        spine(self.a, ("inbound", 5, "delivered", 20), ("outbound", 6, "sent", 1), phone="+260971000005")
        self.assertEqual(spine_quality(self.a, now=NOW)["conversations_with_invalid_ordering"], 1)

    def test_is_account_scoped(self):
        spine(self.b, phone="+260972000001")
        self.assertEqual(spine_quality(self.a, now=NOW)["conversations_with_indeterminate_state"], 0)
        self.assertEqual(spine_quality(self.b, now=NOW)["conversations_with_indeterminate_state"], 1)


class QualityCommandTest(TestCase):
    def setUp(self):
        self.a = Account.objects.create(company_name="A", slug="a")
        self.b = Account.objects.create(company_name="B", slug="b")

    def outbound_log(self, account, phone, status, *, project):
        contact = Contact.objects.create(account=account, phone=phone)
        wa = WhatsAppContact.objects.create(account=account, phone_number=phone, contact=contact)
        wa_convo = WaConversation.get_or_open(wa)
        log = MessageLog.objects.create(
            account=account, conversation=wa_convo, contact=wa, direction="out",
            message_id=f"wamid.{phone}", status=status, timestamp=NOW)
        if project:
            generic = Conversation.get_or_create_for_whatsapp(wa_convo)
            Message.objects.create(account=account, conversation=generic, direction="outbound",
                                   body="x", timestamp=NOW, status=status, whatsapp_message=log)

    def run_cmd(self, *args):
        out = StringIO()
        call_command("conversation_quality", *args, stdout=out)
        return out.getvalue()

    def test_missing_outbound_counts_sent_replies_absent_from_the_spine(self):
        self.outbound_log(self.a, "+260971000001", "sent", project=False)        # missing
        self.outbound_log(self.a, "+260971000002", "delivered", project=False)   # missing
        self.outbound_log(self.a, "+260971000003", "sent", project=True)         # fine
        self.outbound_log(self.a, "+260971000004", "queued", project=False)      # not sent yet: not "missing"
        self.outbound_log(self.b, "+260972000001", "sent", project=False)        # other tenant
        data = json.loads(self.run_cmd("--account", "a", "--json"))
        self.assertEqual(data["accounts"][0]["conversations_with_missing_outbound"], 2)

    def test_all_accounts_are_reported_independently_and_read_only(self):
        self.outbound_log(self.a, "+260971000001", "sent", project=False)
        before = (MessageLog.objects.count(), Message.objects.count(), Conversation.objects.count())
        with CaptureQueriesContext(connection) as ctx:
            data = json.loads(self.run_cmd("--all-accounts", "--json"))
        self.assertEqual({e["account"]["slug"] for e in data["accounts"]}, {"a", "b"})
        by = {e["account"]["slug"]: e for e in data["accounts"]}
        self.assertEqual(by["a"]["conversations_with_missing_outbound"], 1)
        self.assertEqual(by["b"]["conversations_with_missing_outbound"], 0)
        for q in ctx.captured_queries:
            self.assertNotRegex(q["sql"].lstrip().upper(), r"^(INSERT|UPDATE|DELETE|SAVEPOINT)")
        self.assertEqual(before, (MessageLog.objects.count(), Message.objects.count(),
                                  Conversation.objects.count()))

    def test_human_readable_report_and_argument_validation(self):
        report = self.run_cmd("--account", "a")
        for text in ("conversations_with_indeterminate_state", "conversations_with_missing_outbound",
                     "conversations_with_invalid_ordering"):
            self.assertIn(text, report)
        with self.assertRaises(CommandError):
            self.run_cmd("--account", "nope")
