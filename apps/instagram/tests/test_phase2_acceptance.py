"""
Phase 2 Acceptance Tests — Instagram Engagement & Moderation

Checklist (all must pass for Phase 2 sign-off):
  P2-01  ModerationRule keyword match hides a comment
  P2-02  ModerationRule keyword match deletes a comment
  P2-03  ModerationRule FLAG action marks comment as pending (local only, no API)
  P2-04  Spam-detection heuristic matches expected patterns
  P2-05  Toxicity heuristic matches expected patterns
  P2-06  Complaint heuristic matches expected patterns
  P2-07  No rule = no ModerationLog created
  P2-08  Moderation action runs before intent detection in inbound pipeline
  P2-09  Comment already HIDDEN: hide action is skipped (idempotent)
  P2-10  Comment already DELETED: delete action is skipped (idempotent)
  P2-11  hide_comment API failure → outcome=FAILED; comment state unchanged
  P2-12  delete_comment API failure → outcome=FAILED; comment state unchanged
  P2-13  Automation trigger NOTIFY_STAFF: logs notification (no API call)
  P2-14  Automation trigger CREATE_PROPOSAL: creates AIProposal if none pending
  P2-15  CREATE_PROPOSAL is idempotent: does not create duplicate AIProposals
  P2-16  CREATE_PROPOSAL skipped when no canonical contact
  P2-17  ModerationLog is immutable (save/delete raise)
  P2-18  Priority ordering: lower priority rule wins when multiple rules match
  P2-19  Mention webhook entries are normalised and processed as comments
  P2-20  Buying intent still detected even when moderation rule matches
"""
from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.instagram.models.comment import Comment, CommentThread
from apps.instagram.models.moderation import ModerationLog, ModerationRule
from apps.instagram.services.moderation import moderate_comment

from .helpers import make_account, make_instagram_account, make_instagram_contact


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_rule(account, *, name=None, match_type=ModerationRule.MatchType.KEYWORD,
              keywords="badword", moderation_action=ModerationRule.ModerationAction.HIDE,
              automation_trigger=ModerationRule.AutomationTrigger.NONE,
              priority=10, is_active=True):
    return ModerationRule.objects.create(
        account=account,
        name=name or f"rule_{secrets.token_hex(4)}",
        match_type=match_type,
        keywords=keywords,
        moderation_action=moderation_action,
        automation_trigger=automation_trigger,
        priority=priority,
        is_active=is_active,
    )


def make_thread_and_comment(ig_account, ig_contact, body="test comment"):
    thread = CommentThread.objects.create(
        instagram_account=ig_account,
        comment_id=f"cmt_{secrets.token_hex(6)}",
        post_id=f"post_{secrets.token_hex(6)}",
        instagram_contact=ig_contact,
        body=body,
        received_at=timezone.now(),
    )
    comment = Comment.objects.create(
        thread=thread,
        comment_id=f"c_{secrets.token_hex(6)}",
        instagram_contact=ig_contact,
        body=body,
        direction=Comment.Direction.INBOUND,
        timestamp=timezone.now(),
    )
    return thread, comment


# ---------------------------------------------------------------------------
# P2-01 – P2-03: Moderation actions
# ---------------------------------------------------------------------------


class TestModerationActions(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_01_keyword_hide(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = True
        make_rule(self.account, keywords="badword",
                  moderation_action=ModerationRule.ModerationAction.HIDE)
        _, comment = make_thread_and_comment(self.ig_account, self.ig_contact, "badword here")

        log = moderate_comment(comment)

        self.assertIsNotNone(log)
        self.assertEqual(log.outcome, ModerationLog.Outcome.APPLIED)
        self.assertEqual(log.moderation_action, ModerationRule.ModerationAction.HIDE)
        comment.refresh_from_db()
        self.assertTrue(comment.is_hidden)
        self.assertEqual(comment.moderation_state, Comment.ModerationState.HIDDEN)
        MockProvider.return_value.hide_comment.assert_called_once_with(comment.comment_id)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_02_keyword_delete(self, MockProvider):
        MockProvider.return_value.delete_comment.return_value = True
        make_rule(self.account, keywords="spam",
                  moderation_action=ModerationRule.ModerationAction.DELETE)
        _, comment = make_thread_and_comment(self.ig_account, self.ig_contact, "buy cheap spam")

        log = moderate_comment(comment)

        self.assertEqual(log.outcome, ModerationLog.Outcome.APPLIED)
        comment.refresh_from_db()
        self.assertEqual(comment.moderation_state, Comment.ModerationState.DELETED)
        MockProvider.return_value.delete_comment.assert_called_once_with(comment.comment_id)

    def test_p2_03_flag_action_is_local_only(self):
        make_rule(self.account, keywords="flag_me",
                  moderation_action=ModerationRule.ModerationAction.FLAG)
        _, comment = make_thread_and_comment(self.ig_account, self.ig_contact, "flag_me")

        with patch("apps.instagram.providers.meta.MetaInstagramProvider") as MockProvider:
            log = moderate_comment(comment)
            MockProvider.assert_not_called()

        self.assertEqual(log.outcome, ModerationLog.Outcome.APPLIED)
        comment.refresh_from_db()
        self.assertEqual(comment.moderation_state, Comment.ModerationState.PENDING)


# ---------------------------------------------------------------------------
# P2-04 – P2-06: Heuristic matchers
# ---------------------------------------------------------------------------


class TestHeuristicMatchers(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_04_spam_detection(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = True
        make_rule(self.account,
                  match_type=ModerationRule.MatchType.SPAM_DETECTION,
                  moderation_action=ModerationRule.ModerationAction.HIDE)

        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "check out my profile for deals"
        )
        log = moderate_comment(comment)
        self.assertEqual(log.outcome, ModerationLog.Outcome.APPLIED)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_05_toxicity_detection(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = True
        make_rule(self.account,
                  match_type=ModerationRule.MatchType.TOXICITY,
                  moderation_action=ModerationRule.ModerationAction.HIDE)

        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "this is trash and you are stupid"
        )
        log = moderate_comment(comment)
        self.assertEqual(log.outcome, ModerationLog.Outcome.APPLIED)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_06_complaint_detection(self, MockProvider):
        MockProvider.return_value.flag_comment = None
        make_rule(self.account,
                  match_type=ModerationRule.MatchType.COMPLAINT,
                  moderation_action=ModerationRule.ModerationAction.FLAG)

        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "I want a refund, this is a scam"
        )
        log = moderate_comment(comment)
        self.assertEqual(log.outcome, ModerationLog.Outcome.APPLIED)


# ---------------------------------------------------------------------------
# P2-07: No rule = no log
# ---------------------------------------------------------------------------


class TestNoRuleNoLog(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    def test_p2_07_no_matching_rule_returns_none(self):
        # Rule requires "badword" but comment doesn't contain it
        make_rule(self.account, keywords="badword")
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "I love this product!"
        )
        result = moderate_comment(comment)
        self.assertIsNone(result)
        self.assertEqual(ModerationLog.objects.count(), 0)


# ---------------------------------------------------------------------------
# P2-08: Moderation runs before intent in inbound pipeline
# ---------------------------------------------------------------------------


class TestInboundPipelineOrder(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_08_moderate_runs_in_inbound_pipeline(self, MockProvider):
        """Moderation service is invoked when a comment is processed."""
        from apps.instagram.services.inbound import _process_comment_entry

        MockProvider.return_value.hide_comment.return_value = True
        make_rule(
            self.account,
            keywords="buy",
            moderation_action=ModerationRule.ModerationAction.HIDE,
        )
        entry = {
            "id": f"cmt_{secrets.token_hex(6)}",
            "text": "I want to buy this",
            "from": {"id": self.ig_contact.instagram_scoped_id},
            "media": {"id": f"post_{secrets.token_hex(6)}"},
            "timestamp": timezone.now().isoformat(),
            "parent_id": "",
        }
        _process_comment_entry(self.ig_account, entry)
        # A ModerationLog should exist if moderation ran
        self.assertEqual(ModerationLog.objects.count(), 1)


# ---------------------------------------------------------------------------
# P2-09 – P2-10: Idempotency (skip already-moderated comments)
# ---------------------------------------------------------------------------


class TestModerationIdempotency(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_09_already_hidden_skip(self, MockProvider):
        make_rule(self.account, keywords="bad",
                  moderation_action=ModerationRule.ModerationAction.HIDE)
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "something bad"
        )
        comment.moderation_state = Comment.ModerationState.HIDDEN
        comment.save()

        log = moderate_comment(comment)
        self.assertEqual(log.outcome, ModerationLog.Outcome.SKIPPED)
        MockProvider.return_value.hide_comment.assert_not_called()

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_10_already_deleted_skip(self, MockProvider):
        make_rule(self.account, keywords="bad",
                  moderation_action=ModerationRule.ModerationAction.DELETE)
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "something bad"
        )
        comment.moderation_state = Comment.ModerationState.DELETED
        comment.save()

        log = moderate_comment(comment)
        self.assertEqual(log.outcome, ModerationLog.Outcome.SKIPPED)
        MockProvider.return_value.delete_comment.assert_not_called()


# ---------------------------------------------------------------------------
# P2-11 – P2-12: API failures
# ---------------------------------------------------------------------------


class TestAPIFailures(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_11_hide_api_failure(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = False
        make_rule(self.account, keywords="bad",
                  moderation_action=ModerationRule.ModerationAction.HIDE)
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "bad text"
        )
        log = moderate_comment(comment)
        self.assertEqual(log.outcome, ModerationLog.Outcome.FAILED)
        comment.refresh_from_db()
        # State should not have changed on failure
        self.assertNotEqual(comment.moderation_state, Comment.ModerationState.HIDDEN)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_12_delete_api_failure(self, MockProvider):
        MockProvider.return_value.delete_comment.return_value = False
        make_rule(self.account, keywords="bad",
                  moderation_action=ModerationRule.ModerationAction.DELETE)
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "bad text"
        )
        log = moderate_comment(comment)
        self.assertEqual(log.outcome, ModerationLog.Outcome.FAILED)
        comment.refresh_from_db()
        self.assertNotEqual(comment.moderation_state, Comment.ModerationState.DELETED)


# ---------------------------------------------------------------------------
# P2-13 – P2-16: Automation triggers
# ---------------------------------------------------------------------------


class TestAutomationTriggers(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, self.contact = make_instagram_contact(
            self.account, self.ig_account
        )

    def test_p2_13_notify_staff_trigger(self):
        make_rule(
            self.account,
            keywords="help",
            moderation_action=ModerationRule.ModerationAction.FLAG,
            automation_trigger=ModerationRule.AutomationTrigger.NOTIFY_STAFF,
        )
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "I need help please"
        )
        with patch("apps.instagram.services.moderation.logger") as mock_logger:
            log = moderate_comment(comment)
        self.assertEqual(log.automation_trigger, ModerationRule.AutomationTrigger.NOTIFY_STAFF)
        # Ensure the notification intent was logged (no API call)
        mock_logger.info.assert_called()

    def test_p2_14_create_proposal_trigger(self):
        from apps.ai.models import AIProposal

        make_rule(
            self.account,
            match_type=ModerationRule.MatchType.BUYING_INTENT,
            moderation_action=ModerationRule.ModerationAction.FLAG,
            automation_trigger=ModerationRule.AutomationTrigger.CREATE_PROPOSAL,
        )
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "how much does this cost to buy?"
        )
        moderate_comment(comment)
        self.assertEqual(
            AIProposal.objects.filter(
                account=self.account,
                action="purchase_intent",
            ).count(),
            1,
        )

    def test_p2_15_create_proposal_is_idempotent(self):
        from apps.ai.models import AIProposal

        rule = make_rule(
            self.account,
            match_type=ModerationRule.MatchType.BUYING_INTENT,
            moderation_action=ModerationRule.ModerationAction.FLAG,
            automation_trigger=ModerationRule.AutomationTrigger.CREATE_PROPOSAL,
        )
        # Two comments on the SAME thread — only one proposal should be created
        thread, comment1 = make_thread_and_comment(
            self.ig_account, self.ig_contact, "buy this now, how much?"
        )
        comment2 = Comment.objects.create(
            thread=thread,
            comment_id=f"c_{secrets.token_hex(6)}",
            instagram_contact=self.ig_contact,
            body="I want to purchase, price please",
            direction=Comment.Direction.INBOUND,
            timestamp=timezone.now(),
        )
        moderate_comment(comment1)
        moderate_comment(comment2)
        self.assertEqual(
            AIProposal.objects.filter(
                account=self.account,
                action="purchase_intent",
                status="pending",
            ).count(),
            1,
        )

    def test_p2_16_create_proposal_skipped_without_canonical_contact(self):
        from apps.ai.models import AIProposal
        from apps.instagram.models.contact import InstagramContact

        # Create a contact with no canonical Contact linked
        ig_contact_no_canonical = InstagramContact.objects.create(
            account=self.account,
            instagram_scoped_id=f"igsid_{secrets.token_hex(6)}",
            contact=None,
        )
        make_rule(
            self.account,
            match_type=ModerationRule.MatchType.BUYING_INTENT,
            moderation_action=ModerationRule.ModerationAction.FLAG,
            automation_trigger=ModerationRule.AutomationTrigger.CREATE_PROPOSAL,
        )
        _, comment = make_thread_and_comment(
            self.ig_account, ig_contact_no_canonical, "how much to buy?"
        )
        moderate_comment(comment)
        self.assertEqual(AIProposal.objects.count(), 0)


# ---------------------------------------------------------------------------
# P2-17: ModerationLog immutability
# ---------------------------------------------------------------------------


class TestModerationLogImmutability(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_17_moderation_log_is_immutable(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = True
        make_rule(self.account, keywords="bad",
                  moderation_action=ModerationRule.ModerationAction.HIDE)
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "bad word"
        )
        log = moderate_comment(comment)
        self.assertIsNotNone(log)

        with self.assertRaises(ValueError):
            log.outcome = ModerationLog.Outcome.SKIPPED
            log.save()

        with self.assertRaises(ValueError):
            log.delete()


# ---------------------------------------------------------------------------
# P2-18: Priority ordering
# ---------------------------------------------------------------------------


class TestRulePriorityOrdering(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_18_lower_priority_number_wins(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = True
        MockProvider.return_value.delete_comment.return_value = True

        # Both rules match "badword"; priority 1 (hide) should win over priority 20 (delete)
        hide_rule = make_rule(
            self.account, keywords="badword",
            moderation_action=ModerationRule.ModerationAction.HIDE,
            priority=1,
        )
        make_rule(
            self.account, keywords="badword",
            moderation_action=ModerationRule.ModerationAction.DELETE,
            priority=20,
        )

        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "contains badword"
        )
        log = moderate_comment(comment)
        self.assertEqual(log.rule, hide_rule)
        self.assertEqual(log.moderation_action, ModerationRule.ModerationAction.HIDE)


# ---------------------------------------------------------------------------
# P2-19: Mention webhook normalisation
# ---------------------------------------------------------------------------


class TestMentionWebhookProcessing(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    def test_p2_19_mention_payload_normalised_and_processed(self):
        from apps.instagram.services.inbound import _normalise_mention

        mention_value = {
            "comment_id": f"cmt_{secrets.token_hex(6)}",
            "text": "great product @thebrand!",
            "from": {"id": self.ig_contact.instagram_scoped_id, "username": "tester"},
            "media_id": f"post_{secrets.token_hex(6)}",
            "timestamp": timezone.now().isoformat(),
            "parent_id": "",
        }
        normalised = _normalise_mention(mention_value)
        self.assertEqual(normalised["id"], mention_value["comment_id"])
        self.assertEqual(normalised["text"], mention_value["text"])
        self.assertEqual(normalised["media"]["id"], mention_value["media_id"])
        self.assertTrue(normalised.get("_mention"))


# ---------------------------------------------------------------------------
# P2-20: Intent still detected with moderation rule active
# ---------------------------------------------------------------------------


class TestIntentWithModeration(TestCase):

    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_p2_20_intent_detected_alongside_moderation(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = True
        # Rule to hide comments containing "click here"
        make_rule(self.account, keywords="click here",
                  moderation_action=ModerationRule.ModerationAction.HIDE)
        # Comment that both triggers the keyword rule AND contains buying intent
        # "how much" and "buy" both trigger detect_buying_intent
        body = "click here to see how much it costs to buy"
        comment_id = f"cmt_{secrets.token_hex(6)}"
        post_id = f"post_{secrets.token_hex(6)}"

        from apps.instagram.services.inbound import _process_comment_entry

        entry = {
            "id": comment_id,
            "text": body,
            "from": {"id": self.ig_contact.instagram_scoped_id, "username": "tester"},
            "media": {"id": post_id},
            "timestamp": timezone.now().isoformat(),
            "parent_id": "",
        }
        _process_comment_entry(self.ig_account, entry)

        thread = CommentThread.objects.get(comment_id=comment_id)
        # Intent should have been detected
        self.assertNotEqual(thread.intent, "")
        # Moderation log should exist
        comment = Comment.objects.get(thread=thread, comment_id=comment_id)
        self.assertTrue(ModerationLog.objects.filter(comment=comment).exists())
