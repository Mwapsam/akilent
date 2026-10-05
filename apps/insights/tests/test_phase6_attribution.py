"""Phase 6 — action loop + attribution tests.

P6-01  execute_recommendation links conversation and marks accepted
P6-02  execute_recommendation is idempotent — second call is a no-op
P6-03  execute_recommendation back-fills originated_from on existing attributions
P6-04  record_outcome_signal: dm_sent_at recorded
P6-05  record_outcome_signal: idempotent — same signal not overwritten
P6-06  record_outcome_signal: outcome_measured_at set on first signal only
P6-07  record_outcome_signal: revenue + currency stored on order_paid_at
P6-08  record_outcome_signal: unknown signal raises ValueError
P6-09  attach_attribution_provenance wires originated_from onto attribution
P6-10  insight_for_order returns the originating Insight
P6-11  insight_for_order returns None when no attribution exists
P6-12  END-TO-END: high_engagement_contact → Insight → Rec → DM → reply
       → Lead → Order → Order.attribution.originated_from.insight == original Insight
"""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from apps.insights.actions import (
    OUTCOME_SIGNAL_ORDER,
    attach_attribution_provenance,
    execute_recommendation,
    insight_for_order,
    record_outcome_signal,
)
from apps.insights.engine import upsert_insight
from apps.insights.models import Insight, RecommendationLog


def _make_account(name="P6 Co"):
    from apps.accounts.models import Account

    return Account.objects.create(company_name=name)


def _make_contact(account):
    from apps.contacts.models import Contact

    return Contact.objects.create(account=account)


def _make_spine(account, contact, channel="instagram"):
    from apps.conversations.models import Conversation

    return Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )


def _make_insight(account):
    return Insight(
        account=account,
        type="instagram_high_engagement_contact",
        severity=Insight.Severity.OPPORTUNITY,
        title="3 engaged contacts",
        evidence={"count": 3},
        evidence_count=3,
        suggested_action={"action": "instagram_dm_outreach"},
    )


def _make_rec(account, insight=None):
    return RecommendationLog.objects.create(account=account, insight=insight)


# ---------------------------------------------------------------------------
# P6-01 / P6-02  execute_recommendation
# ---------------------------------------------------------------------------


class TestExecuteRecommendation(TestCase):
    def setUp(self):
        self.account = _make_account()
        self.contact = _make_contact(self.account)
        self.spine = _make_spine(self.account, self.contact)
        insight, _ = upsert_insight(_make_insight(self.account))
        self.rec = _make_rec(self.account, insight=insight)

    def test_p601_links_conversation_and_marks_accepted(self):
        execute_recommendation(
            self.rec, conversation=self.spine, action_type="instagram_dm"
        )
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.conversation_id, self.spine.pk)
        self.assertEqual(self.rec.status, RecommendationLog.Status.ACCEPTED)
        self.assertTrue(self.rec.accepted)
        self.assertEqual(self.rec.action_type, "instagram_dm")
        self.assertIsNotNone(self.rec.acted_at)

    def test_p602_idempotent_second_call_noop(self):
        execute_recommendation(
            self.rec, conversation=self.spine, action_type="instagram_dm"
        )
        spine2 = _make_spine(self.account, self.contact)
        execute_recommendation(
            self.rec, conversation=spine2, action_type="instagram_dm"
        )
        self.rec.refresh_from_db()
        # Still points at the first conversation
        self.assertEqual(self.rec.conversation_id, self.spine.pk)

    def test_p603_backfills_originated_from_on_existing_attribution(self):
        from apps.conversations.models import ConversationAttribution
        from apps.crm.models import Lead

        lead = Lead.objects.create(
            account=self.account, contact=self.contact, source="instagram"
        )
        attribution = ConversationAttribution.objects.create(
            account=self.account,
            conversation=self.spine,
            channel="instagram",
            lead=lead,
            method=ConversationAttribution.Method.EXPLICIT,
        )
        self.assertIsNone(attribution.originated_from_id)
        execute_recommendation(
            self.rec, conversation=self.spine, action_type="instagram_dm"
        )
        attribution.refresh_from_db()
        self.assertEqual(attribution.originated_from_id, self.rec.pk)


# ---------------------------------------------------------------------------
# P6-04 through P6-08  record_outcome_signal
# ---------------------------------------------------------------------------


class TestRecordOutcomeSignal(TestCase):
    def setUp(self):
        self.account = _make_account()
        insight, _ = upsert_insight(_make_insight(self.account))
        self.rec = _make_rec(self.account, insight=insight)

    def test_p604_dm_sent_at_recorded(self):
        record_outcome_signal(self.rec, "dm_sent_at")
        self.rec.refresh_from_db()
        self.assertIn("dm_sent_at", self.rec.outcome_summary)
        self.assertTrue(self.rec.outcome_summary["dm_sent_at"])

    def test_p605_same_signal_not_overwritten(self):
        record_outcome_signal(self.rec, "dm_sent_at")
        first_ts = self.rec.outcome_summary["dm_sent_at"]
        record_outcome_signal(self.rec, "dm_sent_at")
        self.assertEqual(self.rec.outcome_summary["dm_sent_at"], first_ts)

    def test_p606_outcome_measured_at_set_on_first_signal_only(self):
        self.assertIsNone(self.rec.outcome_measured_at)
        record_outcome_signal(self.rec, "dm_sent_at")
        self.rec.refresh_from_db()
        first_measured = self.rec.outcome_measured_at
        self.assertIsNotNone(first_measured)
        record_outcome_signal(self.rec, "dm_replied_at")
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.outcome_measured_at, first_measured)

    def test_p607_revenue_and_currency_stored_on_order_paid(self):
        record_outcome_signal(
            self.rec, "order_paid_at", revenue="250.00", currency="ZMW"
        )
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.outcome_summary["revenue"], "250.00")
        self.assertEqual(self.rec.outcome_summary["currency"], "ZMW")

    def test_p608_unknown_signal_raises(self):
        with self.assertRaises(ValueError):
            record_outcome_signal(self.rec, "invented_signal")


# ---------------------------------------------------------------------------
# P6-09  attach_attribution_provenance
# ---------------------------------------------------------------------------


class TestAttachAttributionProvenance(TestCase):
    def test_p609_wires_originated_from(self):
        from apps.conversations.models import ConversationAttribution
        from apps.crm.models import Lead

        account = _make_account()
        contact = _make_contact(account)
        spine = _make_spine(account, contact)
        rec = _make_rec(account)
        lead = Lead.objects.create(account=account, contact=contact, source="instagram")
        attribution = ConversationAttribution.objects.create(
            account=account,
            conversation=spine,
            channel="instagram",
            lead=lead,
            method=ConversationAttribution.Method.EXPLICIT,
        )
        attach_attribution_provenance(attribution, rec)
        attribution.refresh_from_db()
        self.assertEqual(attribution.originated_from_id, rec.pk)

    def test_p609_idempotent(self):
        from apps.conversations.models import ConversationAttribution
        from apps.crm.models import Lead

        account = _make_account()
        contact = _make_contact(account)
        spine = _make_spine(account, contact)
        rec = _make_rec(account)
        rec2 = _make_rec(account)
        lead = Lead.objects.create(account=account, contact=contact, source="instagram")
        attribution = ConversationAttribution.objects.create(
            account=account,
            conversation=spine,
            channel="instagram",
            lead=lead,
            method=ConversationAttribution.Method.EXPLICIT,
            originated_from=rec,
        )
        attach_attribution_provenance(attribution, rec2)
        attribution.refresh_from_db()
        self.assertEqual(attribution.originated_from_id, rec.pk)  # not overwritten


# ---------------------------------------------------------------------------
# P6-10 / P6-11  insight_for_order
# ---------------------------------------------------------------------------


class TestInsightForOrder(TestCase):
    def _make_order(self, account, contact, attribution=None):
        from apps.commerce.models import Order

        order = Order.objects.create(
            account=account,
            contact=contact,
            status=Order.Status.PAID,
        )
        return order

    def test_p610_returns_originating_insight(self):
        from apps.commerce.models import Order
        from apps.conversations.models import ConversationAttribution

        account = _make_account()
        contact = _make_contact(account)
        spine = _make_spine(account, contact)
        insight, _ = upsert_insight(_make_insight(account))
        rec = _make_rec(account, insight=insight)
        order = Order.objects.create(
            account=account, contact=contact, status=Order.Status.PAID
        )
        ConversationAttribution.objects.create(
            account=account,
            conversation=spine,
            channel="instagram",
            order=order,
            method=ConversationAttribution.Method.EXPLICIT,
            originated_from=rec,
        )
        result = insight_for_order(order)
        self.assertEqual(result.pk, insight.pk)

    def test_p611_returns_none_when_no_attribution(self):
        from apps.commerce.models import Order

        account = _make_account()
        contact = _make_contact(account)
        order = Order.objects.create(
            account=account, contact=contact, status=Order.Status.PAID
        )
        self.assertIsNone(insight_for_order(order))


# ---------------------------------------------------------------------------
# P6-12  END-TO-END causal chain test
# ---------------------------------------------------------------------------


class TestEndToEndAttributionChain(TestCase):
    """The full causal journey:

    high_engagement_contact Insight
        ↓ upsert_insight
    Insight saved
        ↓ RecommendationLog presented
    RecommendationLog
        ↓ execute_recommendation (staff clicks "send DM")
    Conversation linked + provenance recorded
        ↓ customer replies → Lead created + attributed
    ConversationAttribution (lead) with originated_from
        ↓ Order created + attributed
    ConversationAttribution (order) with originated_from
        ↓ Order paid → outcome signal recorded
    RecommendationLog.outcome_summary["order_paid_at"] set

    Terminal assertion: Order.attribution.originated_from.insight == original Insight
    """

    def test_p612_full_causal_chain(self):
        from apps.commerce.models import Order
        from apps.conversations.models import ConversationAttribution
        from apps.crm.models import Lead

        account = _make_account("End-to-End Co")

        # --- Detection phase ---
        ig_account, ig_contact = self._setup_instagram(account)
        self._make_threads(ig_account, ig_contact, count=3)

        from apps.insights.rules import rule_instagram_high_engagement_contact

        raw_insight = rule_instagram_high_engagement_contact(account)
        self.assertIsNotNone(raw_insight, "Rule must fire with 3+ threads")
        insight, _ = upsert_insight(raw_insight)

        # --- Recommendation presented ---
        rec = RecommendationLog.objects.create(account=account, insight=insight)
        self.assertEqual(rec.status, RecommendationLog.Status.PRESENTED)

        # --- Staff executes the recommendation (sends DM) ---
        canonical_contact = ig_contact.contact
        spine = _make_spine(account, canonical_contact)
        execute_recommendation(rec, conversation=spine, action_type="instagram_dm")
        rec.refresh_from_db()
        self.assertEqual(rec.conversation_id, spine.pk)
        self.assertEqual(rec.action_type, "instagram_dm")

        record_outcome_signal(rec, "dm_sent_at")

        # --- Customer replies ---
        record_outcome_signal(rec, "dm_replied_at")

        # --- Lead created from conversation ---
        lead = Lead.objects.create(
            account=account,
            contact=canonical_contact,
            conversation=spine,
            source="instagram",
        )
        ConversationAttribution.objects.create(
            account=account,
            conversation=spine,
            channel="instagram",
            lead=lead,
            method=ConversationAttribution.Method.EXPLICIT,
            originated_from=rec,
        )
        record_outcome_signal(rec, "lead_created_at")

        # --- Order created ---
        order = Order.objects.create(
            account=account, contact=canonical_contact, status=Order.Status.PENDING
        )
        ConversationAttribution.objects.create(
            account=account,
            conversation=spine,
            channel="instagram",
            order=order,
            method=ConversationAttribution.Method.EXPLICIT,
            originated_from=rec,
        )
        record_outcome_signal(rec, "order_created_at")

        # --- Order paid ---
        order.status = Order.Status.PAID
        order.save(update_fields=["status"])
        record_outcome_signal(rec, "order_paid_at", revenue="300.00", currency="ZMW")

        rec.refresh_from_db()
        signals = rec.outcome_summary
        for signal in OUTCOME_SIGNAL_ORDER:
            self.assertIn(signal, signals, f"Signal {signal!r} must be recorded")
        self.assertEqual(signals["revenue"], "300.00")
        self.assertIsNotNone(rec.outcome_measured_at)

        # --- The chain is queryable ---
        originating_insight = insight_for_order(order)
        self.assertIsNotNone(originating_insight)
        self.assertEqual(originating_insight.pk, insight.pk)
        self.assertEqual(originating_insight.type, "instagram_high_engagement_contact")

    # ------------------------------------------------------------------

    def _setup_instagram(self, account):
        from apps.instagram.models.account import InstagramBusinessAccount
        from apps.instagram.models.contact import InstagramContact
        from apps.instagram.services.contacts import resolve_or_create_contact

        ig_account = InstagramBusinessAccount.objects.create(
            account=account,
            instagram_business_account_id="ig_p6_001",
            page_id="page_p6_001",
            access_token="tok",
            verify_token="vt",
        )
        ig_contact = InstagramContact.objects.create(
            account=account,
            instagram_scoped_id="igsid_p6_001",
            username="engaged_buyer",
        )
        resolve_or_create_contact(ig_account, "igsid_p6_001")
        ig_contact.refresh_from_db()
        return ig_account, ig_contact

    def _make_threads(self, ig_account, ig_contact, *, count=3):
        from apps.instagram.models.comment import CommentThread

        for i in range(count):
            CommentThread.objects.create(
                instagram_account=ig_account,
                instagram_contact=ig_contact,
                comment_id=f"cid_p6_{i}",
                body="Love this product",
                received_at=timezone.now(),
            )
