"""Inbound WhatsApp messages must reach the Inbox whether or not the beta
automation-events flag is on; the flag only decides if Workflows are enrolled."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.whatsapp.models import MessageLog
from apps.whatsapp.tests.test_auto_reply import Base, PHONE

ENABLED = "apps.whatsapp.tasks._automation_events_enabled"
ENROLL = "apps.conversations.services._enroll_workflows"
REPLY = "apps.whatsapp.tasks._auto_reply_during_setup"


class InboxProjectionTest(Base):
    def setUp(self):
        super().setUp()
        p = patch(REPLY)  # not under test here
        p.start()
        self.addCleanup(p.stop)

    def test_message_reaches_inbox_with_automation_flag_off(self):
        with patch(ENABLED, return_value=False), patch(ENROLL) as enroll:
            self.receive()
        convo = Conversation.objects.get(account=self.account)
        self.assertEqual(convo.channel, Conversation.Channel.WHATSAPP)
        self.assertTrue(convo.is_unread)
        self.assertEqual(convo.contact.phone, PHONE)
        message = Message.objects.get(conversation=convo)
        self.assertEqual((message.body, message.direction), ("Hi", Message.Direction.INBOUND))
        enroll.assert_not_called()  # the flag still gates Workflows

    def test_new_contact_is_named_from_the_whatsapp_profile(self):
        with patch(ENABLED, return_value=False):
            self.receive()
        contact = Contact.objects.get(account=self.account)
        self.assertEqual((contact.phone, contact.first_name, contact.source), (PHONE, "Tester", "whatsapp"))

    def test_existing_contact_name_is_not_overwritten(self):
        Contact.objects.create(account=self.account, phone=PHONE, first_name="Ada")
        with patch(ENABLED, return_value=False):
            self.receive()
        self.assertEqual(Contact.objects.get(account=self.account).first_name, "Ada")

    def test_workflows_are_enrolled_when_the_flag_is_on(self):
        with patch(ENABLED, return_value=True), patch(ENROLL) as enroll:
            self.receive()
        self.assertEqual(Conversation.objects.filter(account=self.account).count(), 1)
        enroll.assert_called_once()

    def test_flag_lookup_failure_still_creates_the_inbox_conversation(self):
        with patch(ENABLED, side_effect=RuntimeError("cache down")), patch(ENROLL) as enroll:
            self.receive()
        self.assertEqual(Conversation.objects.filter(account=self.account).count(), 1)
        enroll.assert_not_called()

    def test_replay_does_not_duplicate(self):
        with patch(ENABLED, return_value=False):
            self.receive(msg_id="wamid.1")
            self.receive(msg_id="wamid.1")
        self.assertEqual(Conversation.objects.filter(account=self.account).count(), 1)
        self.assertEqual(Message.objects.filter(account=self.account).count(), 1)
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 1)

    def test_a_second_message_joins_the_same_conversation(self):
        with patch(ENABLED, return_value=False):
            self.receive(msg_id="wamid.1")
            self.receive(msg_id="wamid.2", text="Anyone there?")
        self.assertEqual(Conversation.objects.filter(account=self.account).count(), 1)
        self.assertEqual(Message.objects.filter(account=self.account).count(), 2)

    def test_projection_failure_never_fails_the_inbound_event(self):
        with patch(ENABLED, return_value=False), patch(
            "apps.conversations.services.record_inbound_whatsapp_message",
            side_effect=RuntimeError("boom"),
        ):
            self.receive()
        self.assertEqual(MessageLog.objects.filter(direction="in").count(), 1)
        self.assertEqual(Conversation.objects.filter(account=self.account).count(), 0)

    def test_a_replay_repairs_an_earlier_failed_projection(self):
        with patch(ENABLED, return_value=False), patch(
            "apps.conversations.services.record_inbound_whatsapp_message",
            side_effect=RuntimeError("boom"),
        ):
            self.receive(msg_id="wamid.1")
        with patch(ENABLED, return_value=False):
            self.receive(msg_id="wamid.1")
        self.assertEqual(Conversation.objects.filter(account=self.account).count(), 1)


class BackfillInboxTest(Base):
    def setUp(self):
        super().setUp()
        for target in (REPLY, ENABLED):
            p = patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, *args):
        out = StringIO()
        call_command("backfill_inbox", *args, stdout=out)
        return out.getvalue()

    def test_recreates_inbox_conversations_for_already_logged_messages(self):
        self.receive(msg_id="wamid.1")
        Message.objects.all().delete()
        Conversation.objects.all().delete()

        self.assertIn("Backfilled 1 message", self._run())
        self.assertEqual(Conversation.objects.filter(account=self.account).count(), 1)
        self.assertEqual(Message.objects.filter(account=self.account).count(), 1)

    def test_is_safe_to_rerun(self):
        self.receive(msg_id="wamid.1")
        Message.objects.all().delete()
        Conversation.objects.all().delete()
        self._run()
        self.assertIn("Backfilled 0 message(s); 1 already", self._run())
        self.assertEqual(Message.objects.filter(account=self.account).count(), 1)

    def test_links_whatsapp_contacts_that_never_got_a_contact(self):
        from apps.whatsapp.models import WhatsAppContact

        wa = WhatsAppContact.objects.create(account=self.account, phone_number=PHONE, display_name="Tester")
        self.assertIsNone(wa.contact)
        self.assertIn("Linked 1 WhatsApp contact(s)", self._run())
        wa.refresh_from_db()
        self.assertEqual((wa.contact.phone, wa.contact.first_name), (PHONE, "Tester"))
        self.assertIn("Linked 0 WhatsApp contact(s)", self._run())  # re-run is a no-op

    def test_never_starts_workflows(self):
        self.receive(msg_id="wamid.1")
        Message.objects.all().delete()
        Conversation.objects.all().delete()
        with patch(ENROLL) as enroll:
            self._run()
        enroll.assert_not_called()

    def test_account_filter(self):
        self.receive(msg_id="wamid.1")
        Message.objects.all().delete()
        Conversation.objects.all().delete()
        self.assertIn("Backfilled 0 message(s)", self._run("--account", str(self.account.pk + 999)))
        self.assertEqual(Conversation.objects.count(), 0)
