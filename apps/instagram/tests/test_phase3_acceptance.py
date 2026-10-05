"""
Phase 3 Acceptance Tests — Comment → Private Reply → DM

Checklist (5 invariants from spec + supporting cases):
  P3-01  Keyword trigger fires: private reply enqueued and sent, DM conversation opened
  P3-02  Buying-intent trigger fires on buying-intent comment
  P3-03  Private reply idempotency: 5 webhook deliveries → 1 OutboundMessage → 1 private reply
  P3-04  Seven-day window: private_reply() is a distinct provider operation (not send_message)
  P3-05  Conversation only created after successful Meta API call
  P3-06  No duplicate conversations: repeated trigger → existing conversation reused
  P3-07  Comment attribution chain preserved: Lead → Conversation → InstagramConversation → CommentThread
  P3-08  trigger_fired_at set atomically; concurrent delivery does not double-fire
  P3-09  Reply-comments (parent_id set) never fire triggers
  P3-10  No active trigger → no private reply, no conversation
  P3-11  trigger_fired_at already set → trigger skipped without calling provider
  P3-12  reply_template {username} interpolation works
  P3-13  Failed API call: OutboundMessage is FAILED; no conversation opened
  P3-14  ANY_COMMENT trigger fires on every root comment body
  P3-15  Priority ordering: lower priority trigger wins when multiple match
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.instagram.models.comment import CommentThread
from apps.instagram.models.message import OutboundMessage
from apps.instagram.models.trigger import CommentTrigger
from apps.instagram.services.triggers import evaluate_triggers

from .helpers import make_account, make_instagram_account, make_instagram_contact

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_trigger(
    account,
    *,
    name=None,
    match_type=CommentTrigger.MatchType.KEYWORD,
    keywords="buy",
    reply_template="Hi {username}, DM us!",
    priority=10,
    is_active=True,
):
    return CommentTrigger.objects.create(
        account=account,
        name=name or f"trigger_{secrets.token_hex(4)}",
        match_type=match_type,
        keywords=keywords,
        reply_template=reply_template,
        priority=priority,
        is_active=is_active,
    )


def make_thread(
    ig_account, ig_contact, body="", *, comment_id=None, trigger_fired=False
):
    thread = CommentThread.objects.create(
        instagram_account=ig_account,
        comment_id=comment_id or f"cmt_{secrets.token_hex(6)}",
        post_id=f"post_{secrets.token_hex(6)}",
        instagram_contact=ig_contact,
        body=body,
        received_at=timezone.now(),
        trigger_fired_at=timezone.now() if trigger_fired else None,
    )
    return thread


# ---------------------------------------------------------------------------
# P3-01: Keyword trigger fires
# ---------------------------------------------------------------------------


class TestKeywordTrigger(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_01_keyword_trigger_fires(self, MockProvider):
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=True, provider_message_id="mid_123", error="", terminal=False
        )

        make_trigger(self.account, keywords="buy,price")
        thread = make_thread(
            self.ig_account, self.ig_contact, "how much does it cost to buy?"
        )

        result = evaluate_triggers(thread, "how much does it cost to buy?")

        self.assertTrue(result)
        # OutboundMessage created
        outbound = OutboundMessage.objects.get(instagram_account=self.ig_account)
        self.assertEqual(outbound.action_type, OutboundMessage.ActionType.PRIVATE_REPLY)
        self.assertEqual(outbound.status, OutboundMessage.Status.SENT)
        # Conversation opened
        thread.refresh_from_db()
        self.assertIsNotNone(thread.conversation_id)
        # Private reply API called (not send_message)
        instance.private_reply.assert_called_once()
        instance.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# P3-02: Buying-intent trigger
# ---------------------------------------------------------------------------


class TestBuyingIntentTrigger(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_02_buying_intent_trigger(self, MockProvider):
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=True, provider_message_id="mid_bi", error="", terminal=False
        )

        make_trigger(self.account, match_type=CommentTrigger.MatchType.BUYING_INTENT)
        thread = make_thread(self.ig_account, self.ig_contact, "how much is this?")

        result = evaluate_triggers(thread, "how much is this?")

        self.assertTrue(result)
        instance.private_reply.assert_called_once()


# ---------------------------------------------------------------------------
# P3-03: Private reply idempotency
# ---------------------------------------------------------------------------


class TestPrivateReplyIdempotency(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_03_five_deliveries_one_send(self, MockProvider):
        """5 webhook deliveries → 1 OutboundMessage → 1 Meta call."""
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=True, provider_message_id="mid_idem", error="", terminal=False
        )

        make_trigger(self.account, keywords="price")
        body = "what is the price?"
        thread = make_thread(self.ig_account, self.ig_contact, body)

        # Simulate 5 webhook deliveries
        for _ in range(5):
            # Re-fetch thread to simulate separate request
            fresh_thread = CommentThread.objects.get(pk=thread.pk)
            evaluate_triggers(fresh_thread, body)

        # Exactly one OutboundMessage
        self.assertEqual(
            OutboundMessage.objects.filter(instagram_account=self.ig_account).count(), 1
        )
        # Meta called at most once (first delivery sends; subsequent ones skip
        # because trigger_fired_at is already set)
        self.assertEqual(instance.private_reply.call_count, 1)


# ---------------------------------------------------------------------------
# P3-04: private_reply is a distinct provider operation
# ---------------------------------------------------------------------------


class TestPrivateReplyDistinctOperation(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_04_private_reply_not_send_message(self, MockProvider):
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=True, provider_message_id="mid_pr", error="", terminal=False
        )

        make_trigger(self.account, keywords="order")
        thread = make_thread(self.ig_account, self.ig_contact, "I want to order")

        evaluate_triggers(thread, "I want to order")

        instance.private_reply.assert_called_once()
        instance.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# P3-05: Conversation only created after successful API call
# ---------------------------------------------------------------------------


class TestConversationCreationTiming(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_05_no_conversation_on_api_failure(self, MockProvider):
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=False, provider_message_id="", error="API down", terminal=False
        )

        make_trigger(self.account, keywords="buy")
        thread = make_thread(self.ig_account, self.ig_contact, "want to buy")

        evaluate_triggers(thread, "want to buy")

        thread.refresh_from_db()
        # No conversation opened on failure — this is the critical invariant
        self.assertIsNone(thread.conversation_id)
        # OutboundMessage exists and is in a failed/retry state (not SENT)
        outbound = OutboundMessage.objects.get(instagram_account=self.ig_account)
        self.assertNotEqual(outbound.status, OutboundMessage.Status.SENT)


# ---------------------------------------------------------------------------
# P3-06: No duplicate conversations
# ---------------------------------------------------------------------------


class TestNoDuplicateConversations(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_06_repeated_trigger_reuses_conversation(self, MockProvider):
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=True, provider_message_id="mid_c1", error="", terminal=False
        )

        make_trigger(self.account, keywords="buy")

        # First comment thread fires trigger
        thread1 = make_thread(self.ig_account, self.ig_contact, "buy this")
        evaluate_triggers(thread1, "buy this")
        thread1.refresh_from_db()
        conv1_id = thread1.conversation_id
        self.assertIsNotNone(conv1_id)

        # Second comment on same contact — same conversation should be reused
        instance.private_reply.return_value = MagicMock(
            success=True, provider_message_id="mid_c2", error="", terminal=False
        )
        thread2 = make_thread(self.ig_account, self.ig_contact, "I want to buy more")
        evaluate_triggers(thread2, "I want to buy more")
        thread2.refresh_from_db()

        # Same spine conversation
        self.assertEqual(thread2.conversation_id, conv1_id)

        # Only one InstagramConversation
        from apps.instagram.models.conversation import InstagramConversation

        self.assertEqual(
            InstagramConversation.objects.filter(
                instagram_contact=self.ig_contact
            ).count(),
            1,
        )


# ---------------------------------------------------------------------------
# P3-07: Attribution chain preserved
# ---------------------------------------------------------------------------


class TestAttributionChain(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, self.contact = make_instagram_contact(
            self.account, self.ig_account
        )

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_07_lead_traceable_to_comment(self, MockProvider):
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=True, provider_message_id="mid_attr", error="", terminal=False
        )

        make_trigger(self.account, keywords="buy")
        thread = make_thread(self.ig_account, self.ig_contact, "I want to buy!")

        evaluate_triggers(thread, "I want to buy!")
        thread.refresh_from_db()

        self.assertIsNotNone(thread.conversation_id)

        # The spine conversation links to InstagramConversation which links to contact
        from apps.conversations.models import Conversation

        spine = Conversation.objects.get(pk=thread.conversation_id)
        self.assertIsNotNone(spine.instagram_conversation)
        ig_convo = spine.instagram_conversation
        self.assertEqual(ig_convo.instagram_contact, self.ig_contact)

        # Staff confirms → Lead
        from apps.crm.models import Lead

        Lead.objects.create(
            account=self.account,
            contact=self.contact,
            source="instagram",
            conversation=spine,
        )
        lead = Lead.objects.get(contact=self.contact, source="instagram")
        # Attribution chain: Lead → Conversation → InstagramConversation → CommentThread
        self.assertEqual(lead.conversation, spine)
        self.assertEqual(spine.instagram_conversation, ig_convo)
        self.assertEqual(
            ig_convo.instagram_contact.comment_threads.filter(pk=thread.pk).count(), 1
        )


# ---------------------------------------------------------------------------
# P3-08: trigger_fired_at atomicity (concurrent delivery)
# ---------------------------------------------------------------------------


class TestTriggerFiredAtAtomicity(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_08_concurrent_delivery_one_winner(self, MockProvider):
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=True, provider_message_id="mid_conc", error="", terminal=False
        )

        make_trigger(self.account, keywords="price")
        thread = make_thread(self.ig_account, self.ig_contact, "what is the price")

        # Simulate two concurrent deliveries both seeing trigger_fired_at=None.
        # Only one UPDATE wins; the other returns updated=0.
        from apps.instagram.services.triggers import _claim_trigger

        won1 = _claim_trigger(thread)
        # Re-fetch to get current DB state
        fresh = CommentThread.objects.get(pk=thread.pk)
        won2 = _claim_trigger(fresh)

        self.assertTrue(won1)
        self.assertFalse(won2)


# ---------------------------------------------------------------------------
# P3-09: Reply-comments never fire triggers
# ---------------------------------------------------------------------------


class TestReplyCommentsNoTrigger(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_09_reply_comment_no_trigger(self, MockProvider):
        from apps.instagram.services.inbound import _process_comment_entry

        make_trigger(self.account, keywords="buy")
        parent_comment_id = f"cmt_{secrets.token_hex(6)}"
        # First create the parent thread so get_or_create works
        CommentThread.objects.create(
            instagram_account=self.ig_account,
            comment_id=parent_comment_id,
            post_id=f"post_{secrets.token_hex(6)}",
            instagram_contact=self.ig_contact,
            body="original comment",
            received_at=timezone.now(),
        )
        # A reply (has parent_id set)
        entry = {
            "id": f"cmt_{secrets.token_hex(6)}",
            "text": "I want to buy this product",
            "from": {"id": self.ig_contact.instagram_scoped_id},
            "media": {"id": f"post_{secrets.token_hex(6)}"},
            "timestamp": timezone.now().isoformat(),
            "parent_id": parent_comment_id,
        }
        _process_comment_entry(self.ig_account, entry)

        MockProvider.return_value.private_reply.assert_not_called()
        self.assertEqual(OutboundMessage.objects.count(), 0)


# ---------------------------------------------------------------------------
# P3-10: No active trigger → no private reply
# ---------------------------------------------------------------------------


class TestNoActiveTrigger(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_10_no_trigger_no_outbound(self, MockProvider):
        # No triggers configured at all
        thread = make_thread(self.ig_account, self.ig_contact, "I want to buy")

        result = evaluate_triggers(thread, "I want to buy")

        self.assertFalse(result)
        self.assertEqual(OutboundMessage.objects.count(), 0)
        MockProvider.return_value.private_reply.assert_not_called()


# ---------------------------------------------------------------------------
# P3-11: trigger_fired_at already set → skipped without provider call
# ---------------------------------------------------------------------------


class TestAlreadyFiredTrigger(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_11_already_fired_no_provider_call(self, MockProvider):
        make_trigger(self.account, keywords="buy")
        thread = make_thread(
            self.ig_account, self.ig_contact, "want to buy", trigger_fired=True
        )

        result = evaluate_triggers(thread, "want to buy")

        self.assertFalse(result)
        MockProvider.return_value.private_reply.assert_not_called()
        self.assertEqual(OutboundMessage.objects.count(), 0)


# ---------------------------------------------------------------------------
# P3-12: reply_template {username} interpolation
# ---------------------------------------------------------------------------


class TestReplyTemplateInterpolation(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)

    def test_p3_12_username_interpolation(self):
        trigger = CommentTrigger(
            account=self.account,
            name="test",
            match_type=CommentTrigger.MatchType.KEYWORD,
            reply_template="Hi {username}, check your DMs!",
        )
        self.assertEqual(trigger.render_reply("alice"), "Hi alice, check your DMs!")
        self.assertEqual(trigger.render_reply(""), "Hi there, check your DMs!")


# ---------------------------------------------------------------------------
# P3-13: API failure → FAILED outbound, no conversation
# ---------------------------------------------------------------------------
# (Covered by P3-05 above — separate class for clarity in the test report)


class TestApiFailureNoConversation(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_13_terminal_api_failure(self, MockProvider):
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=False, provider_message_id="", error="perm denied", terminal=True
        )

        make_trigger(self.account, keywords="order")
        thread = make_thread(self.ig_account, self.ig_contact, "want to order")

        evaluate_triggers(thread, "want to order")

        outbound = OutboundMessage.objects.get(instagram_account=self.ig_account)
        self.assertEqual(outbound.status, OutboundMessage.Status.FAILED)
        thread.refresh_from_db()
        self.assertIsNone(thread.conversation_id)


# ---------------------------------------------------------------------------
# P3-14: ANY_COMMENT trigger
# ---------------------------------------------------------------------------


class TestAnyCommentTrigger(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_14_any_comment_trigger_fires(self, MockProvider):
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=True, provider_message_id="mid_any", error="", terminal=False
        )

        make_trigger(self.account, match_type=CommentTrigger.MatchType.ANY_COMMENT)
        thread = make_thread(self.ig_account, self.ig_contact, "great product!")

        result = evaluate_triggers(thread, "great product!")

        self.assertTrue(result)
        instance.private_reply.assert_called_once()


# ---------------------------------------------------------------------------
# P3-15: Priority ordering
# ---------------------------------------------------------------------------


class TestTriggerPriorityOrdering(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p3_15_lower_priority_number_wins(self, MockProvider):
        instance = MockProvider.return_value
        instance.can_send.return_value = MagicMock(eligible=True)
        instance.private_reply.return_value = MagicMock(
            success=True, provider_message_id="mid_prio", error="", terminal=False
        )

        low_priority = make_trigger(
            self.account,
            keywords="buy",
            reply_template="LOW priority reply",
            priority=1,
        )
        make_trigger(
            self.account,
            keywords="buy",
            reply_template="HIGH priority reply",
            priority=20,
        )

        thread = make_thread(self.ig_account, self.ig_contact, "want to buy")
        evaluate_triggers(thread, "want to buy")

        # Check the body sent to provider matches the low-priority (winning) trigger
        call_args = instance.private_reply.call_args
        sent_body = call_args[0][1]  # positional: private_reply(comment_id, body)
        self.assertEqual(sent_body, low_priority.render_reply())
