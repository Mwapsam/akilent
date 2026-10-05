"""
Phase 4 Acceptance Tests — Attribution & Analytics

Checklist:
  P4-01  instagram_funnel returns zero dict when no Instagram data for the account
  P4-02  CommentThreads in period are counted as comments_received
  P4-03  Threads with trigger_fired_at set are counted as threads_triggered
  P4-04  Threads with a linked conversation are counted as dms_opened
  P4-05  Leads with source='instagram' are counted as leads_confirmed
  P4-06  Paid orders with instagram attribution are counted; revenue is summed per currency
  P4-07  period_metrics includes 'instagram' key with funnel sub-dict
  P4-08  CommentThread outside the period is excluded from all counts
  P4-09  Conversion rates are calculated correctly (comment_to_dm, dm_to_lead, lead_to_order)
  P4-10  Zero denominator in conversion rate returns 0, not an exception
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.conversations import reporting
from apps.conversations.models import Conversation, ConversationAttribution
from apps.conversations.reporting import Period, instagram_funnel
from apps.instagram.models.comment import CommentThread

from .helpers import make_account, make_instagram_account, make_instagram_contact

NOW = timezone.now()
PERIOD = Period(NOW - timedelta(days=7), NOW + timedelta(minutes=1))


def _period_dict(start=None, end=None):
    return Period(start or PERIOD.start, end or PERIOD.end)


def make_thread(ig_account, ig_contact, *, received_at=None, trigger_fired=False, conversation=None):
    thread = CommentThread.objects.create(
        instagram_account=ig_account,
        comment_id=f"cmt_{secrets.token_hex(6)}",
        instagram_contact=ig_contact,
        body="test comment",
        received_at=received_at or NOW,
        trigger_fired_at=NOW if trigger_fired else None,
        conversation=conversation,
    )
    return thread


def make_instagram_conversation(account, ig_account, ig_contact):
    """Create an InstagramConversation + spine Conversation for the contact."""
    from apps.instagram.services.conversations import get_or_create_instagram_conversation
    ig_convo, spine = get_or_create_instagram_conversation(ig_contact)
    return ig_convo, spine


def make_lead(account, contact, source="instagram", *, created_at=None):
    from apps.crm.models import Lead
    lead = Lead.objects.create(
        account=account,
        contact=contact,
        source=source,
    )
    if created_at:
        Lead.objects.filter(pk=lead.pk).update(created_at=created_at)
        lead.refresh_from_db()
    return lead


def make_paid_order(account, contact, conversation, *, total="50.00", currency="USD"):
    from apps.commerce.models import Order
    order = Order.objects.create(
        account=account,
        contact=contact,
        conversation=conversation,
        status=Order.Status.PAID,
        total=Decimal(total),
        currency=currency,
        paid_at=NOW,
    )
    ConversationAttribution.objects.create(
        account=account,
        conversation=conversation,
        channel=Conversation.Channel.INSTAGRAM,
        order=order,
        method=ConversationAttribution.Method.EXPLICIT,
    )
    return order


# ---------------------------------------------------------------------------
# P4-01  Zero dict when no Instagram data
# ---------------------------------------------------------------------------

class TestP401EmptyFunnel(TestCase):
    def test_returns_zeros_with_no_instagram_data(self):
        account, _ = make_account()
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["comments_received"], 0)
        self.assertEqual(result["threads_triggered"], 0)
        self.assertEqual(result["dms_opened"], 0)
        self.assertEqual(result["leads_confirmed"], 0)
        self.assertEqual(result["paid_orders"], 0)
        self.assertEqual(result["revenue"], [])
        # pct() returns None when denominator is 0 ("nothing to measure")
        self.assertIsNone(result["conversion"]["comment_to_dm"])


# ---------------------------------------------------------------------------
# P4-02  CommentThreads in period counted
# ---------------------------------------------------------------------------

class TestP402CommentsReceived(TestCase):
    def test_threads_in_period_counted(self):
        account, _ = make_account()
        ig_account = make_instagram_account(account)
        ig_contact, _ = make_instagram_contact(account, ig_account)
        make_thread(ig_account, ig_contact)
        make_thread(ig_account, ig_contact)
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["comments_received"], 2)

    def test_other_account_threads_excluded(self):
        account, _ = make_account()
        account2, _ = make_account()
        ig_account = make_instagram_account(account)
        ig_account2 = make_instagram_account(account2)
        ig_contact, _ = make_instagram_contact(account, ig_account)
        ig_contact2, _ = make_instagram_contact(account2, ig_account2)
        make_thread(ig_account, ig_contact)
        make_thread(ig_account2, ig_contact2)
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["comments_received"], 1)


# ---------------------------------------------------------------------------
# P4-03  triggered threads counted
# ---------------------------------------------------------------------------

class TestP403ThreadsTriggered(TestCase):
    def test_triggered_threads_counted_separately(self):
        account, _ = make_account()
        ig_account = make_instagram_account(account)
        ig_contact, _ = make_instagram_contact(account, ig_account)
        make_thread(ig_account, ig_contact, trigger_fired=False)
        make_thread(ig_account, ig_contact, trigger_fired=True)
        make_thread(ig_account, ig_contact, trigger_fired=True)
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["comments_received"], 3)
        self.assertEqual(result["threads_triggered"], 2)


# ---------------------------------------------------------------------------
# P4-04  DMs opened (conversation linked) counted
# ---------------------------------------------------------------------------

class TestP404DmsOpened(TestCase):
    def test_threads_with_conversation_counted(self):
        account, _ = make_account()
        ig_account = make_instagram_account(account)
        ig_contact, _ = make_instagram_contact(account, ig_account)
        _, spine = make_instagram_conversation(account, ig_account, ig_contact)
        make_thread(ig_account, ig_contact, conversation=spine)
        make_thread(ig_account, ig_contact)  # no conversation
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["dms_opened"], 1)


# ---------------------------------------------------------------------------
# P4-05  Leads with source='instagram' counted
# ---------------------------------------------------------------------------

class TestP405LeadsConfirmed(TestCase):
    def test_instagram_leads_counted(self):
        account, _ = make_account()
        ig_account = make_instagram_account(account)
        ig_contact1, contact1 = make_instagram_contact(account, ig_account)
        ig_contact2, contact2 = make_instagram_contact(account, ig_account)
        make_lead(account, contact1, source="instagram")
        make_lead(account, contact2, source="whatsapp")  # excluded — different source
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["leads_confirmed"], 1)


# ---------------------------------------------------------------------------
# P4-06  Paid orders with instagram attribution counted; revenue summed
# ---------------------------------------------------------------------------

class TestP406PaidOrders(TestCase):
    def test_paid_order_with_instagram_attribution_counted(self):
        account, _ = make_account()
        ig_account = make_instagram_account(account)
        ig_contact, contact = make_instagram_contact(account, ig_account)
        _, spine = make_instagram_conversation(account, ig_account, ig_contact)
        make_paid_order(account, contact, spine, total="75.00", currency="ZMW")
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["paid_orders"], 1)
        self.assertEqual(len(result["revenue"]), 1)
        self.assertEqual(result["revenue"][0]["currency"], "ZMW")
        # SQLite Sum may return integer-like Decimal; compare via Decimal
        self.assertEqual(Decimal(result["revenue"][0]["total"]), Decimal("75.00"))

    def test_non_instagram_attributed_order_excluded(self):
        account, _ = make_account()
        from apps.contacts.models import Contact
        from apps.commerce.models import Order
        contact = Contact.objects.create(account=account)
        conv = Conversation.objects.create(account=account, contact=contact, channel="whatsapp")
        order = Order.objects.create(
            account=account, contact=contact, conversation=conv,
            status=Order.Status.PAID, total=Decimal("50.00"), currency="USD", paid_at=NOW,
        )
        ConversationAttribution.objects.create(
            account=account, conversation=conv, channel="whatsapp",
            order=order, method=ConversationAttribution.Method.EXPLICIT,
        )
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["paid_orders"], 0)


# ---------------------------------------------------------------------------
# P4-07  period_metrics includes 'instagram' key
# ---------------------------------------------------------------------------

class TestP407PeriodMetrics(TestCase):
    def test_period_metrics_has_instagram_key(self):
        account, _ = make_account()
        result = reporting.period_metrics(account, PERIOD.start, PERIOD.end)
        self.assertIn("instagram", result)
        ig = result["instagram"]
        self.assertIn("comments_received", ig)
        self.assertIn("threads_triggered", ig)
        self.assertIn("dms_opened", ig)
        self.assertIn("leads_confirmed", ig)
        self.assertIn("paid_orders", ig)
        self.assertIn("revenue", ig)
        self.assertIn("conversion", ig)


# ---------------------------------------------------------------------------
# P4-08  Threads outside the period are excluded
# ---------------------------------------------------------------------------

class TestP408PeriodBoundaries(TestCase):
    def test_thread_before_period_excluded(self):
        account, _ = make_account()
        ig_account = make_instagram_account(account)
        ig_contact, _ = make_instagram_contact(account, ig_account)
        old_time = NOW - timedelta(days=30)
        make_thread(ig_account, ig_contact, received_at=old_time)
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["comments_received"], 0)

    def test_thread_in_period_included(self):
        account, _ = make_account()
        ig_account = make_instagram_account(account)
        ig_contact, _ = make_instagram_contact(account, ig_account)
        make_thread(ig_account, ig_contact, received_at=NOW)
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["comments_received"], 1)


# ---------------------------------------------------------------------------
# P4-09  Conversion rates calculated correctly
# ---------------------------------------------------------------------------

class TestP409ConversionRates(TestCase):
    def test_conversion_rates_computed(self):
        account, _ = make_account()
        ig_account = make_instagram_account(account)
        ig_contact, contact = make_instagram_contact(account, ig_account)
        _, spine = make_instagram_conversation(account, ig_account, ig_contact)
        # 4 comments, 2 triggered, 1 with DM, 1 lead
        make_thread(ig_account, ig_contact)
        make_thread(ig_account, ig_contact)
        make_thread(ig_account, ig_contact, trigger_fired=True)
        make_thread(ig_account, ig_contact, trigger_fired=True, conversation=spine)
        make_lead(account, contact, source="instagram")
        result = instagram_funnel(account, PERIOD)
        self.assertEqual(result["comments_received"], 4)
        self.assertEqual(result["threads_triggered"], 2)
        self.assertEqual(result["dms_opened"], 1)
        self.assertEqual(result["leads_confirmed"], 1)
        # 1/4 = 25%
        self.assertEqual(result["conversion"]["comment_to_dm"], 25)
        # 1/1 = 100%
        self.assertEqual(result["conversion"]["dm_to_lead"], 100)
        # 0/1 = 0%
        self.assertEqual(result["conversion"]["lead_to_order"], 0)


# ---------------------------------------------------------------------------
# P4-10  Zero denominator returns 0
# ---------------------------------------------------------------------------

class TestP410ZeroDenominator(TestCase):
    def test_zero_comments_gives_none_conversion(self):
        account, _ = make_account()
        result = instagram_funnel(account, PERIOD)
        # pct() returns None when denominator is 0 ("nothing to measure")
        self.assertIsNone(result["conversion"]["comment_to_dm"])
        self.assertIsNone(result["conversion"]["dm_to_lead"])
        self.assertIsNone(result["conversion"]["lead_to_order"])
