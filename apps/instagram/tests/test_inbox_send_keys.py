"""Gaps G1 and G2 on Instagram: an inbox reply sent again with the same key is never sent a
second time, and the thread records who sent it (apps/conversations/inbox_keys.py)."""

import secrets
from unittest.mock import patch

from django.test import TestCase

from apps.conversations.actions import ActionError, run_action
from apps.conversations.inbox_keys import inbox_key
from apps.conversations.models import Conversation, Message
from apps.instagram.models import OutboundMessage
from apps.instagram.providers.base import SendResult
from apps.instagram.tests.test_messaging_fixes import InstagramCase, _deliver

SEND = "apps.instagram.providers.meta.MetaInstagramProvider.send_message"
KEY = "9d1c7a52-2b3e-4f60-8a71-0b1c2d3e4f50"


class TestInboxSendKeys(InstagramCase):
    def setUp(self):
        super().setUp()
        _deliver(self.dm("Is this in stock?"))
        self.conversation = Conversation.objects.get(channel="instagram")
        self.key = inbox_key(self.conversation, self.user, KEY)

    def reply(self):
        return run_action(
            "reply",
            {"account": self.account},
            conversation=self.conversation,
            body="Yes!",
            idempotency_key=self.key,
        )

    def test_sent_again_returns_the_first_send_without_sending(self):
        with patch(
            SEND, return_value=SendResult(success=True, provider_message_id="mid.k1")
        ) as send:
            first = self.reply()
            second = self.reply()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(first["outbound_message_id"], second["outbound_message_id"])

        message = Message.objects.get(pk=first["outbound_message_id"])
        self.assertEqual(message.metadata["sent_by_user"], self.user.pk)
        self.assertEqual(message.metadata["client_key"], KEY)
        self.assertNotIn("sent_by", message.metadata)

    def _first_attempt_left(self, status, error=""):
        OutboundMessage.objects.create(
            instagram_account=self.ig_account,
            idempotency_key=self.key,
            recipient_igsid=self.igsid,
            action_type=OutboundMessage.ActionType.DM_REPLY,
            body="Yes!",
            status=status,
            last_error=error,
        )

    def test_a_first_attempt_still_sending_is_not_sent_again(self):
        self._first_attempt_left(OutboundMessage.Status.SENDING)
        with patch(SEND) as send:
            result = self.reply()
        send.assert_not_called()
        self.assertTrue(result["queued"])

    def test_an_unconfirmed_first_attempt_stays_unconfirmed(self):
        self._first_attempt_left(OutboundMessage.Status.UNCONFIRMED)
        with patch(SEND) as send:
            result = self.reply()
        send.assert_not_called()
        self.assertTrue(result["unconfirmed"])

    def test_a_failed_first_attempt_says_why(self):
        self._first_attempt_left(OutboundMessage.Status.FAILED, "Window closed")
        with (
            patch(SEND) as send,
            self.assertRaisesMessage(ActionError, "Window closed"),
        ):
            self.reply()
        send.assert_not_called()


class TestOtherSendersUnchanged(TestCase):
    """Keys that aren't from the inbox keep the old behaviour (a queued row is sent now)."""

    def test_non_inbox_keys_are_not_inbox_keys(self):
        from apps.conversations.inbox_keys import is_inbox_key

        self.assertFalse(is_inbox_key(f"ai-auto:{secrets.token_hex(4)}"))
        self.assertFalse(is_inbox_key(""))
        self.assertTrue(is_inbox_key(f"inbox:1:2:{KEY}"))
