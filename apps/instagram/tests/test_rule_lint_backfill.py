"""Comment rules that overlap or catch too much are pointed out, can be merged, and can be
applied to older comments: moderation only, never a message, email or lead."""

from __future__ import annotations

import datetime
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import AccountFeatureOverride
from apps.instagram.models import CommentTrigger
from apps.instagram.models.comment import Comment
from apps.instagram.models.moderation import ModerationLog, ModerationRule, RuleChannels
from apps.instagram.services import backfill
from apps.instagram.services.moderation import moderate_comment
from apps.instagram.services.rule_lint import apply_merge, lint_rules, merge_preview

from .helpers import make_account, make_instagram_account, make_instagram_contact
from .test_phase2_acceptance import make_rule, make_thread_and_comment

A = ModerationRule.ModerationAction
T = ModerationRule.AutomationTrigger
M = ModerationRule.MatchType
SEND = "apps.email.services.send.send_system_email"
HIDE = "apps.instagram.providers.meta.MetaInstagramProvider.hide_comment"
DELETE = "apps.instagram.providers.meta.MetaInstagramProvider.delete_comment"


def _kinds(result, pk):
    return sorted(w.kind for w in result.warnings if w.rule_pk == pk)


class LintTest(TestCase):
    def setUp(self):
        self.account, _ = make_account()

    def test_the_audits_duplicate_spam_rules(self):
        """ "Spam and scams" first, then a hand-made "Spam Protection" with some of its words."""
        make_rule(
            self.account,
            name="Spam and scams",
            match_type=M.SPAM_DETECTION,
            keywords="DM me, crypto, double your money",
            priority=10,
        )
        later = make_rule(
            self.account,
            name="Spam Protection",
            match_type=M.KEYWORD,
            keywords="crypto, double your money",
            priority=20,
        )
        result = lint_rules(self.account)
        assert _kinds(result, later.pk) == ["shadowed"]
        warning = result.for_rule("moderation", later.pk)[0]
        assert "Never acts" in warning.message and "Spam and scams" in warning.message
        assert warning.other_name == "Spam and scams"

    def test_complaint_keywords_overlap_the_complaints_patterns(self):
        first = make_rule(
            self.account,
            name="Complaint Keywords",
            keywords="refund, scam, worst",
            moderation_action=A.FLAG,
            priority=10,
        )
        later = make_rule(
            self.account,
            name="Complaints",
            match_type=M.COMPLAINT,
            keywords="",
            moderation_action=A.FLAG,
            priority=20,
        )
        result = lint_rules(self.account)
        assert _kinds(result, later.pk) == ["overlap"]
        assert _kinds(result, first.pk) == []

    def test_rules_on_different_channels_dont_overlap(self):
        ig = make_rule(self.account, keywords="crypto", priority=10)
        ig.channels = RuleChannels.INSTAGRAM
        ig.save()
        fb = make_rule(self.account, keywords="crypto", priority=20)
        fb.channels = RuleChannels.FACEBOOK
        fb.save()
        assert lint_rules(self.account).warnings == []

    def test_paused_rules_are_left_out(self):
        make_rule(self.account, keywords="crypto", priority=10, is_active=False)
        later = make_rule(self.account, keywords="crypto", priority=20)
        assert _kinds(lint_rules(self.account), later.pk) == []

    def test_potential_lead_gets_a_soft_note_not_a_warning(self):
        """A lead rule on service/help/link/contact tags most comments (the audit's case)."""
        rule = make_rule(
            self.account,
            name="Potential lead",
            keywords="service, help, link, contact, price",
            moderation_action=A.NONE,
            automation_trigger=T.CREATE_PROPOSAL,
        )
        result = lint_rules(self.account)
        [warning] = result.for_rule("moderation", rule.pk)
        assert warning.kind == "broad" and warning.severity == "note"
        assert "“service”" in warning.message and "“price”" not in warning.message
        assert result.page_level == []

    def test_a_hide_rule_on_common_words_is_a_warning(self):
        rule = make_rule(self.account, keywords="price, dm")
        [warning] = lint_rules(self.account).for_rule("moderation", rule.pk)
        assert warning.severity == "warning"
        assert "real customers' comments" in warning.message
        assert "“price”" in warning.message

    def test_short_keywords_say_where_they_also_match(self):
        rule = make_rule(self.account, keywords="hi", moderation_action=A.FLAG)
        [warning] = lint_rules(self.account).for_rule("moderation", rule.pk)
        assert "“this”" in warning.message

    def test_an_auto_reply_on_price_is_risky(self):
        trigger = CommentTrigger.objects.create(
            account=self.account,
            name="Price",
            match_type=CommentTrigger.MatchType.KEYWORD,
            keywords="price",
            reply_template="Sent you the price",
        )
        [warning] = lint_rules(self.account).for_rule("reply", trigger.pk)
        assert warning.severity == "warning"
        assert "answer almost every comment" in warning.message

    def test_good_rules_have_no_warnings(self):
        make_rule(self.account, keywords="double your money, join my crypto group")
        make_rule(
            self.account,
            keywords="refund please",
            moderation_action=A.FLAG,
            priority=20,
        )
        assert lint_rules(self.account).warnings == []


class MergeTest(TestCase):
    def setUp(self):
        self.account, self.user = make_account()
        make_instagram_account(self.account)
        AccountFeatureOverride.objects.create(
            account=self.account, key="instagram", grant=True, note="test"
        )
        self.client.force_login(self.user)
        self.target = make_rule(
            self.account, name="Spam and scams", keywords="DM me, crypto"
        )
        self.source = make_rule(
            self.account, name="Spam Protection", keywords="crypto, Forex", priority=20
        )
        self.source.channels = RuleChannels.FACEBOOK
        self.source.save()
        self.target.channels = RuleChannels.INSTAGRAM
        self.target.save()

    def test_preview_shows_before_and_after_without_changing_anything(self):
        preview = merge_preview(self.source, self.target)
        assert preview["keywords"] == "DM me, crypto, Forex"
        assert preview["added_keywords"] == ["Forex"]
        assert preview["channels"] == RuleChannels.BOTH and preview["channels_changed"]
        response = self.client.get(
            f"/instagram/rules/moderation/{self.source.pk}/merge/?into={self.target.pk}"
        )
        self.assertContains(response, "DM me, crypto, Forex")
        self.assertContains(response, "Adds: Forex")
        self.target.refresh_from_db()
        assert self.target.keywords == "DM me, crypto"

    def test_merge_turns_the_source_off_and_never_deletes_it(self):
        response = self.client.post(
            f"/instagram/rules/moderation/{self.source.pk}/merge/",
            {"into": self.target.pk},
        )
        assert response.status_code == 302
        self.target.refresh_from_db()
        self.source.refresh_from_db()
        assert self.target.keywords == "DM me, crypto, Forex"
        assert self.target.channels == RuleChannels.BOTH
        assert self.source.is_active is False
        assert ModerationRule.objects.filter(pk=self.source.pk).exists()

    def test_merging_into_another_workspaces_rule_is_refused(self):
        other_account, _ = make_account()
        other = make_rule(other_account, keywords="x")
        response = self.client.post(
            f"/instagram/rules/moderation/{self.source.pk}/merge/", {"into": other.pk}
        )
        assert response.status_code == 404
        other.refresh_from_db()
        assert other.keywords == "x"

    def test_the_rules_page_offers_the_merge(self):
        self.source.channels = RuleChannels.BOTH
        self.source.keywords = "crypto"
        self.source.save()
        self.target.channels = RuleChannels.BOTH
        self.target.save()
        response = self.client.get("/instagram/rules/")
        self.assertContains(response, "1 rule needs a look")
        self.assertContains(response, f"merge/?into={self.target.pk}")

    def test_apply_merge_is_idempotent_on_keywords(self):
        apply_merge(self.source, self.target)
        apply_merge(self.source, self.target)
        self.target.refresh_from_db()
        assert self.target.keywords == "DM me, crypto, Forex"


class ModerationOnlyTest(TestCase):
    """Applying rules to older comments: hide or flag, and nothing else."""

    def setUp(self):
        patcher = patch(SEND)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        self.account, self.user = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    def _comment(self, body, days_ago=3):
        _, comment = make_thread_and_comment(self.ig_account, self.ig_contact, body)
        comment.timestamp = timezone.now() - datetime.timedelta(days=days_ago)
        comment.save(update_fields=["timestamp"])
        return comment

    def test_a_flag_rule_flags_without_emailing(self):
        make_rule(
            self.account,
            keywords="refund",
            moderation_action=A.FLAG,
            automation_trigger=T.NOTIFY_STAFF,
        )
        comment = self._comment("I want a refund")
        log = moderate_comment(comment, moderation_only=True)
        comment.refresh_from_db()
        assert comment.moderation_state == Comment.ModerationState.FLAGGED
        assert log.backfill and not log.emailed and log.automation_trigger == ""
        self.send.assert_not_called()

    def test_a_lead_rule_opens_no_lead_and_leaves_no_log(self):
        make_rule(
            self.account,
            keywords="price",
            moderation_action=A.NONE,
            automation_trigger=T.CREATE_PROPOSAL,
        )
        comment = self._comment("what's the price")
        with patch("apps.instagram.services.moderation._open_lead") as open_lead:
            assert moderate_comment(comment, moderation_only=True) is None
        open_lead.assert_not_called()
        assert not ModerationLog.objects.exists()

    def test_a_delete_rule_hides_instead(self):
        make_rule(self.account, keywords="crypto", moderation_action=A.DELETE)
        comment = self._comment("Join a crypto group")
        with patch(HIDE, return_value=(True, "")) as hide, patch(DELETE) as delete:
            log = moderate_comment(comment, moderation_only=True)
        hide.assert_called_once()
        delete.assert_not_called()
        assert (
            log.moderation_action == "hide" and log.display_outcome["label"] == "Hidden"
        )

    def test_new_comments_still_email_and_record_it(self):
        make_rule(self.account, keywords="refund", moderation_action=A.FLAG)
        log = moderate_comment(self._comment("refund now", days_ago=0))
        assert log.emailed and not log.backfill
        assert log.display_trail == ["team emailed"]

    def test_the_trail_never_says_none(self):
        make_rule(
            self.account,
            keywords="price",
            moderation_action=A.NONE,
            automation_trigger=T.CREATE_PROPOSAL,
        )
        with patch("apps.instagram.services.moderation._open_lead"):
            log = moderate_comment(self._comment("price?", days_ago=0))
        assert log.display_outcome is None
        assert log.display_trail == ["Lead"]


class ApplyToExistingTest(TestCase):
    def setUp(self):
        patcher = patch(SEND)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        cache.clear()
        self.account, self.user = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)
        AccountFeatureOverride.objects.create(
            account=self.account, key="instagram", grant=True, note="test"
        )
        self.client.force_login(self.user)
        make_rule(
            self.account,
            name="Spam",
            keywords="crypto, double your money",
            moderation_action=A.HIDE,
        )
        make_rule(
            self.account,
            name="Complaints",
            keywords="refund",
            moderation_action=A.FLAG,
            priority=20,
        )
        make_rule(
            self.account,
            name="Lead",
            keywords="price",
            moderation_action=A.NONE,
            automation_trigger=T.CREATE_PROPOSAL,
            priority=30,
        )

    def _comment(self, body, days_ago=5):
        _, comment = make_thread_and_comment(self.ig_account, self.ig_contact, body)
        comment.timestamp = timezone.now() - datetime.timedelta(days=days_ago)
        comment.save(update_fields=["timestamp"])
        return comment

    def test_preview_counts_what_would_happen_and_changes_nothing(self):
        self._comment("double your money in two days")
        self._comment("Join a crypto group")
        self._comment("I want a refund")
        self._comment("what's the price")
        self._comment("lovely")
        self._comment("crypto too old", days_ago=45)
        already = self._comment("crypto, already hidden")
        already.moderation_state = Comment.ModerationState.HIDDEN
        already.save()
        counts = backfill.preview(self.account)
        assert (counts["checked"], counts["hide"], counts["flag"]) == (5, 2, 1)
        assert not ModerationLog.objects.exists()
        response = self.client.get("/instagram/rules/apply-existing/")
        self.assertContains(response, "Checks 5 comments")
        self.assertContains(response, "~2</strong> will be hidden")
        self.assertContains(response, "No messages or emails are sent")

    def test_apply_hides_old_spam_and_sends_nothing(self):
        spam = self._comment("double your money in two days")
        complaint = self._comment("I want a refund")
        lead = self._comment("what's the price")
        with (
            patch(HIDE, return_value=(True, "")) as hide,
            patch("apps.instagram.services.moderation._open_lead") as open_lead,
            patch("apps.instagram.services.triggers.evaluate_triggers") as triggers,
        ):
            response = self.client.post("/instagram/rules/apply-existing/")
        assert response.status_code == 302
        hide.assert_called_once_with(spam.comment_id)
        open_lead.assert_not_called()
        triggers.assert_not_called()
        self.send.assert_not_called()
        spam.refresh_from_db()
        complaint.refresh_from_db()
        assert spam.moderation_state == Comment.ModerationState.HIDDEN
        assert complaint.moderation_state == Comment.ModerationState.FLAGGED
        assert not lead.moderation_logs.exists()
        assert all(log.backfill for log in ModerationLog.objects.all())
        status = cache.get(backfill.status_key(self.account.pk))
        assert status == {
            "state": "done",
            "checked": 3,
            "hidden": 1,
            "flagged": 1,
            "failed": 0,
        }
        page = self.client.get("/instagram/rules/")
        self.assertContains(page, "hid 1, flagged 1")
        self.assertContains(page, "older comment")

    def test_running_twice_acts_once(self):
        self._comment("double your money")
        with patch(HIDE, return_value=(True, "")) as hide:
            self.client.post("/instagram/rules/apply-existing/")
            self.client.post("/instagram/rules/apply-existing/")
        hide.assert_called_once()

    def test_a_second_run_while_one_is_going_is_refused(self):
        cache.add(backfill.lock_key(self.account.pk), 1)
        with patch("apps.instagram.tasks.apply_rules_to_existing.delay") as delay:
            self.client.post("/instagram/rules/apply-existing/")
        delay.assert_not_called()
