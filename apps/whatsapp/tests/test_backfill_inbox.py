"""backfill_inbox recreates both directions, so answered customers are not left "waiting"."""
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.conversations.models import Conversation as Spine
from apps.conversations.models import Message
from apps.conversations.state import get_conversation_state
from apps.whatsapp.models import Conversation, MessageLog, WhatsAppContact


class BackfillOutboundTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")
        self.wa = WhatsAppContact.objects.create(account=self.account, phone_number="+260971903744")
        self.conversation = Conversation.get_or_open(self.wa)
        now = timezone.now()
        for direction, offset, status, mid in (
            (MessageLog.Direction.INBOUND, 60, MessageLog.Status.DELIVERED, "wamid.in"),
            (MessageLog.Direction.OUTBOUND, 30, MessageLog.Status.SENT, "wamid.out"),
        ):
            MessageLog.objects.create(
                account=self.account, conversation=self.conversation, contact=self.wa,
                direction=direction, message_id=mid, content="x", status=status,
                timestamp=now - timedelta(minutes=offset),
            )

    def test_outbound_is_projected_so_the_customer_is_not_waiting(self):
        call_command("backfill_inbox", verbosity=0)
        self.assertEqual(Message.objects.filter(direction="inbound").count(), 1)
        self.assertEqual(Message.objects.filter(direction="outbound").count(), 1)
        self.assertFalse(get_conversation_state(Spine.objects.get()).needs_attention)

    def test_rerun_does_not_duplicate(self):
        call_command("backfill_inbox", verbosity=0)
        call_command("backfill_inbox", verbosity=0)
        self.assertEqual(Message.objects.count(), 2)
