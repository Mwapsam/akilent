"""Phase 5 — Instagram Intelligence insight rules.

IG-01  instagram_unanswered_intent: no threads → None
IG-02  instagram_unanswered_intent: intent threads without DM → Insight created
IG-03  instagram_unanswered_intent: threads with intent AND a DM conversation → excluded
IG-04  instagram_unanswered_intent: threads outside 7-day window → excluded
IG-05  instagram_unanswered_intent: upsert updates evidence on re-run
IG-06  instagram_unanswered_intent: severity=WARNING when count >= 10
IG-07  instagram_high_engagement_contact: no threads → None
IG-08  instagram_high_engagement_contact: contacts with < 3 threads → excluded
IG-09  instagram_high_engagement_contact: contacts with 3+ threads → Insight created
IG-10  instagram_high_engagement_contact: threads outside 14-day window → excluded
IG-11  run_all_rules includes both Instagram rules without error
IG-12  both insight types appear in BusinessPolicy.INSIGHT_TYPE_TO_TRIGGER
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.insights.engine import run_all_rules, upsert_insight
from apps.insights.models import BusinessPolicy, Insight
from apps.insights.rules import (
    rule_instagram_high_engagement_contact,
    rule_instagram_unanswered_intent,
)


def _make_account():
    from apps.accounts.models import Account

    return Account.objects.create(company_name="IG Insights Co")


def _make_ig_account(account):
    from apps.instagram.models.account import InstagramBusinessAccount

    return InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id=f"igba_{account.pk}",
        page_id=f"page_{account.pk}",
        access_token="tok",
        verify_token="vt",
    )


def _make_ig_contact(account, username="buyer"):
    from apps.instagram.models.contact import InstagramContact

    return InstagramContact.objects.create(
        account=account,
        instagram_scoped_id=f"igsid_{username}_{account.pk}",
        username=username,
    )


def _make_thread(ig_account, ig_contact, *, intent="", conversation=None, days_ago=1):
    from apps.instagram.models.comment import CommentThread

    return CommentThread.objects.create(
        instagram_account=ig_account,
        instagram_contact=ig_contact,
        comment_id=f"cid_{ig_account.pk}_{ig_contact.pk}_{days_ago}_{intent[:4]}",
        body="I want this product",
        intent=intent,
        conversation=conversation,
        received_at=timezone.now() - timedelta(days=days_ago),
    )


class TestInstagramUnansweredIntent(TestCase):
    def setUp(self):
        self.account = _make_account()
        self.ig_account = _make_ig_account(self.account)
        self.ig_contact = _make_ig_contact(self.account)

    # IG-01
    def test_no_threads_returns_none(self):
        result = rule_instagram_unanswered_intent(self.account)
        self.assertIsNone(result)

    # IG-02
    def test_intent_thread_without_dm_produces_insight(self):
        _make_thread(self.ig_account, self.ig_contact, intent="buy now")
        result = rule_instagram_unanswered_intent(self.account)
        self.assertIsNotNone(result)
        self.assertEqual(result.type, "instagram_unanswered_intent")
        self.assertEqual(result.evidence_count, 1)
        self.assertEqual(result.evidence["count"], 1)

    # IG-03
    def test_thread_with_dm_conversation_excluded(self):
        from apps.contacts.models import Contact
        from apps.conversations.models import Conversation

        contact = Contact.objects.create(account=self.account)
        spine = Conversation.objects.create(
            account=self.account,
            contact=contact,
            channel=Conversation.Channel.INSTAGRAM,
        )
        _make_thread(
            self.ig_account, self.ig_contact, intent="buy now", conversation=spine
        )
        result = rule_instagram_unanswered_intent(self.account)
        self.assertIsNone(result)

    # IG-04
    def test_thread_outside_7_day_window_excluded(self):
        _make_thread(self.ig_account, self.ig_contact, intent="buy now", days_ago=8)
        result = rule_instagram_unanswered_intent(self.account)
        self.assertIsNone(result)

    # IG-05
    def test_upsert_updates_evidence_on_rerun(self):
        _make_thread(self.ig_account, self.ig_contact, intent="buy now")
        first = rule_instagram_unanswered_intent(self.account)
        upsert_insight(first)

        ig_contact2 = _make_ig_contact(self.account, username="buyer2")
        _make_thread(self.ig_account, ig_contact2, intent="how much")
        second = rule_instagram_unanswered_intent(self.account)
        _, created = upsert_insight(second)

        self.assertFalse(created)
        saved = Insight.objects.get(
            account=self.account, type="instagram_unanswered_intent"
        )
        self.assertEqual(saved.evidence_count, 2)

    # IG-06
    def test_severity_warning_when_count_ge_10(self):
        for i in range(10):
            c = _make_ig_contact(self.account, username=f"user{i}")
            _make_thread(self.ig_account, c, intent="want it", days_ago=1)
        result = rule_instagram_unanswered_intent(self.account)
        self.assertIsNotNone(result)
        self.assertEqual(result.severity, Insight.Severity.WARNING)

    def test_severity_opportunity_when_count_lt_10(self):
        _make_thread(self.ig_account, self.ig_contact, intent="want it")
        result = rule_instagram_unanswered_intent(self.account)
        self.assertIsNotNone(result)
        self.assertEqual(result.severity, Insight.Severity.OPPORTUNITY)


class TestInstagramHighEngagementContact(TestCase):
    def setUp(self):
        self.account = _make_account()
        self.ig_account = _make_ig_account(self.account)

    # IG-07
    def test_no_threads_returns_none(self):
        result = rule_instagram_high_engagement_contact(self.account)
        self.assertIsNone(result)

    # IG-08
    def test_contacts_with_fewer_than_3_threads_excluded(self):
        contact = _make_ig_contact(self.account, "low")
        _make_thread(self.ig_account, contact, days_ago=1)
        _make_thread(self.ig_account, contact, days_ago=2)
        result = rule_instagram_high_engagement_contact(self.account)
        self.assertIsNone(result)

    # IG-09
    def test_contacts_with_3_or_more_threads_produce_insight(self):
        contact = _make_ig_contact(self.account, "high")
        for i in range(3):
            _make_thread(self.ig_account, contact, days_ago=i + 1)
        result = rule_instagram_high_engagement_contact(self.account)
        self.assertIsNotNone(result)
        self.assertEqual(result.type, "instagram_high_engagement_contact")
        self.assertEqual(result.evidence_count, 1)
        self.assertEqual(result.evidence["sample"][0]["username"], "high")
        self.assertEqual(result.evidence["sample"][0]["thread_count"], 3)

    # IG-10
    def test_threads_outside_14_day_window_excluded(self):
        contact = _make_ig_contact(self.account, "old")
        for i in range(3):
            _make_thread(self.ig_account, contact, days_ago=15 + i)
        result = rule_instagram_high_engagement_contact(self.account)
        self.assertIsNone(result)

    def test_only_contacts_in_window_count_toward_threshold(self):
        contact = _make_ig_contact(self.account, "mixed")
        # 2 within window, 2 outside — should not reach threshold of 3
        _make_thread(self.ig_account, contact, days_ago=1)
        _make_thread(self.ig_account, contact, days_ago=2)
        _make_thread(self.ig_account, contact, days_ago=15)
        _make_thread(self.ig_account, contact, days_ago=20)
        result = rule_instagram_high_engagement_contact(self.account)
        self.assertIsNone(result)

    def test_multiple_contacts_both_counted(self):
        for username in ("alpha", "beta"):
            c = _make_ig_contact(self.account, username)
            for i in range(3):
                _make_thread(self.ig_account, c, days_ago=i + 1)
        result = rule_instagram_high_engagement_contact(self.account)
        self.assertIsNotNone(result)
        self.assertEqual(result.evidence_count, 2)


class TestRunAllRulesIncludesInstagram(TestCase):
    # IG-11
    def test_run_all_rules_runs_instagram_rules_without_error(self):
        account = _make_account()
        ig_account = _make_ig_account(account)
        ig_contact = _make_ig_contact(account)
        _make_thread(ig_account, ig_contact, intent="buy now")
        for i in range(3):
            c = _make_ig_contact(account, username=f"eng{i}")
            for j in range(3):
                _make_thread(ig_account, c, days_ago=j + 1)

        summary = run_all_rules(account)
        self.assertEqual(summary["errors"], 0)
        self.assertGreaterEqual(summary["created"] + summary["updated"], 2)
        types = list(
            Insight.objects.filter(account=account).values_list("type", flat=True)
        )
        self.assertIn("instagram_unanswered_intent", types)
        self.assertIn("instagram_high_engagement_contact", types)


class TestBusinessPolicyMapping(TestCase):
    # IG-12
    def test_instagram_types_in_insight_type_to_trigger(self):
        mapping = BusinessPolicy.INSIGHT_TYPE_TO_TRIGGER
        self.assertIn("instagram_unanswered_intent", mapping)
        self.assertIn("instagram_high_engagement_contact", mapping)
