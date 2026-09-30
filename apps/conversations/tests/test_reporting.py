"""Business Health reporting: response, recovery, peak hours, automation impact, sales, channels,
opportunities at risk and the proof bar. See ``apps.conversations.reporting``."""

from datetime import UTC, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts import business_hours
from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import reporting
from apps.conversations.models import (
    Conversation,
    ConversationAttribution,
    FollowUp,
    Message,
)
from apps.crm.models import Lead

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND
NOW = timezone.now()


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


def _contact(account, phone=None):
    phone = phone or f"+26097{Contact.objects.count():07d}"
    return Contact.objects.create(account=account, phone=phone)


def enquiry(
    account,
    at,
    *,
    reply_after=None,
    status="sent",
    sent_by="",
    channel="whatsapp",
    phone=None,
    assigned_to=None,
):
    contact = _contact(account, phone)
    conv = Conversation.objects.create(
        account=account, contact=contact, channel=channel, assigned_to=assigned_to
    )
    Message.objects.create(
        account=account, conversation=conv, direction=IN, body="Price?", timestamp=at
    )
    if reply_after is not None:
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="K50",
            timestamp=at + reply_after,
            status=status,
            metadata={"sent_by": sent_by} if sent_by else {},
        )
    return conv


@pytest.mark.django_db
class TestFirstRepliesAndResponse:
    def test_failed_and_queued_sends_are_not_replies(self, account):
        start = NOW - timedelta(days=1)
        enquiry(account, start, reply_after=timedelta(minutes=5), status="failed")
        enquiry(account, start, reply_after=timedelta(minutes=5), status="queued")
        enquiry(account, start, reply_after=timedelta(minutes=5), status="delivered")
        period = reporting.Period(start - timedelta(hours=1), NOW)
        resp = reporting.response(account, period)
        assert resp["enquiries"] == 3
        assert resp["answered"] == 1
        assert resp["median_first_reply_seconds"] == 300

    def test_no_reply_is_unanswered_not_zero(self, account):
        start = NOW - timedelta(days=1)
        enquiry(account, start)
        period = reporting.Period(start - timedelta(hours=1), NOW)
        resp = reporting.response(account, period)
        assert resp["answered"] == 0
        assert resp["answered_pct"] == 0
        assert resp["median_first_reply_seconds"] is None

    def test_reply_after_24h_counts_as_unanswered(self, account):
        start = NOW - timedelta(days=2)
        enquiry(account, start, reply_after=timedelta(hours=30))
        period = reporting.Period(start - timedelta(hours=1), NOW)
        resp = reporting.response(account, period)
        assert resp["answered"] == 0
        assert resp["unanswered"] == 1

    def test_person_automation_ai_split(self, account):
        start = NOW - timedelta(hours=3)
        enquiry(account, start, reply_after=timedelta(minutes=1), sent_by="")
        enquiry(account, start, reply_after=timedelta(minutes=1), sent_by="automation")
        enquiry(account, start, reply_after=timedelta(minutes=1), sent_by="ai")
        enquiry(account, start, reply_after=timedelta(minutes=1), sent_by="system")
        period = reporting.Period(start - timedelta(hours=1), NOW)
        rows = reporting.first_replies(account, period.start, period.end)
        by = sorted(r["replied_by"] for r in rows)
        assert by == ["", "ai", "automation", "system"]
        impact = reporting.automation_impact(account, period, rows)
        assert impact["first_replies_automatic"] == 2  # automation + ai, not system
        assert impact["first_replies_by_ai"] == 1

    def test_pct_never_invents_a_rate_from_nothing(self):
        assert reporting.pct(0, 0) is None
        assert reporting.pct(2, 4) == 50


@pytest.mark.django_db
class TestResolution:
    def test_median_resolution_time_within_the_period(self, account):
        start = NOW - timedelta(days=2)
        a = enquiry(account, start)
        a.closed_at = start + timedelta(hours=1)
        a.save(update_fields=["closed_at"])
        b = enquiry(account, start)
        b.closed_at = start + timedelta(hours=3)
        b.save(update_fields=["closed_at"])
        period = reporting.Period(start - timedelta(hours=1), NOW)
        res = reporting.resolution(account, period)
        assert res["resolved"] == 2
        assert res["median_resolution_seconds"] == 2 * 3600  # median of 1h, 3h

    def test_still_open_conversations_are_not_counted(self, account):
        start = NOW - timedelta(days=1)
        enquiry(account, start)  # never closed
        period = reporting.Period(start - timedelta(hours=1), NOW)
        res = reporting.resolution(account, period)
        assert res["resolved"] == 0
        assert res["median_resolution_seconds"] is None

    def test_a_conversation_closed_outside_the_period_is_excluded(self, account):
        start = NOW - timedelta(days=10)
        a = enquiry(account, start)
        a.closed_at = start + timedelta(hours=1)  # long before the period below
        a.save(update_fields=["closed_at"])
        period = reporting.Period(NOW - timedelta(days=1), NOW)
        res = reporting.resolution(account, period)
        assert res["resolved"] == 0


@pytest.mark.django_db
class TestRecovery:
    def test_recovery_chain_missed_answered_lead_paid(self, account):
        start = NOW - timedelta(days=2)
        conv = enquiry(account, start)  # never replied yet
        reminded_at = start + timedelta(hours=25)
        FollowUp.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            due_at=reminded_at,
            source=FollowUp.Source.MISSED,
        )
        FollowUp.objects.filter(conversation=conv).update(created_at=reminded_at)
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="Sorry for the delay!",
            status="sent",
            timestamp=reminded_at + timedelta(minutes=10),
        )
        lead = Lead.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
        )
        Lead.objects.filter(pk=lead.pk).update(
            created_at=reminded_at + timedelta(minutes=20)
        )
        from apps.commerce.models import Order

        order = Order.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            status=Order.Status.PAID,
            currency="USD",
            total=Decimal("10.00"),
        )
        Order.objects.filter(pk=order.pk).update(
            paid_at=reminded_at + timedelta(minutes=30)
        )

        period = reporting.Period(start, NOW)
        rec = reporting.recovery(account, period)
        assert rec["missed"] == 1
        assert rec["recovered"] == 1
        assert rec["rate_pct"] == 100
        assert rec["became_leads"] == 1
        assert rec["paid_orders"] == 1

    def test_a_reply_before_the_reminder_does_not_count(self, account):
        start = NOW - timedelta(days=2)
        conv = enquiry(account, start, reply_after=timedelta(minutes=1))
        reminder = FollowUp.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            due_at=NOW,
            source=FollowUp.Source.MISSED,
        )
        # created_at is auto_now_add; set the reminder's real time via update().
        FollowUp.objects.filter(pk=reminder.pk).update(
            created_at=start + timedelta(hours=25)
        )
        period = reporting.Period(start, NOW)
        rec = reporting.recovery(account, period)
        assert rec["missed"] == 1
        assert rec["recovered"] == 0
        assert rec["rate_pct"] == 0

    def test_no_missed_conversations_is_a_dash_not_zero_percent(self, account):
        period = reporting.Period(NOW - timedelta(days=7), NOW)
        rec = reporting.recovery(account, period)
        assert rec["missed"] == 0
        assert rec["rate_pct"] is None


@pytest.mark.django_db
class TestPeakHours:
    def test_below_threshold_is_not_enough(self, account):
        start = NOW - timedelta(days=1)
        for i in range(5):
            enquiry(account, start + timedelta(minutes=i))
        period = reporting.Period(start - timedelta(hours=1), NOW)
        peak = reporting.peak_hours(account, period)
        assert peak["enough"] is False

    def test_busiest_window_and_timezone(self, account):
        import zoneinfo

        business_hours.save_hours(
            account, tz="Africa/Lusaka", schedule={}
        )  # UTC+2, no DST
        start = NOW - timedelta(days=1)
        zone = zoneinfo.ZoneInfo("Africa/Lusaka")
        # 25 enquiries at 20:00 local time (peak), one per minute back to 19:36, so hour 19 gets
        # 24 and hour 20 gets 1 — deliberately not a round number, so a tie with the filler
        # events below (placed at local hour 2, nowhere near the peak) can't happen.
        base_local = (
            (NOW - timedelta(days=1))
            .astimezone(zone)
            .replace(hour=20, minute=0, second=0, microsecond=0)
        )
        for i in range(25):
            at = base_local.astimezone(UTC) - timedelta(minutes=i)
            enquiry(account, at)
        # 5 filler enquiries at local 02:00, built the same way as the peak ones so the test
        # never depends on the wall-clock time it happens to run at.
        filler_local = base_local.replace(hour=2)
        for i in range(5):
            at = filler_local.astimezone(UTC) - timedelta(minutes=i)
            enquiry(account, at)
        period = reporting.Period(start - timedelta(days=2), NOW)
        peak = reporting.peak_hours(account, period)
        assert peak["enough"] is True
        assert peak["total"] == 30
        assert peak["start_hour"] in (19, 20)  # the 2h window containing 20:00


@pytest.mark.django_db
class TestSalesAndChannels:
    def test_currencies_are_never_summed(self, account):
        from apps.commerce.models import Order

        conv = enquiry(
            account, NOW - timedelta(days=1), reply_after=timedelta(minutes=1)
        )
        Order.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            status=Order.Status.PAID,
            currency="USD",
            total=Decimal("10.00"),
            paid_at=NOW - timedelta(hours=12),
        )
        Order.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            status=Order.Status.PAID,
            currency="ZMW",
            total=Decimal("200.00"),
            paid_at=NOW - timedelta(hours=11),
        )
        period = reporting.Period(NOW - timedelta(days=2), NOW)
        rev = reporting.revenue(account, period)
        by_currency = {r["currency"]: r["total"] for r in rev}
        assert by_currency == {"USD": Decimal("10.00"), "ZMW": Decimal("200.00")}

    def test_lead_to_sale_via_contact(self, account):
        from apps.commerce.models import Order

        conv = enquiry(
            account, NOW - timedelta(days=3), reply_after=timedelta(minutes=1)
        )
        lead = Lead.objects.create(
            account=account, contact=conv.contact, conversation=conv
        )
        Lead.objects.filter(pk=lead.pk).update(created_at=NOW - timedelta(days=2))
        Order.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            status=Order.Status.PAID,
            currency="USD",
            total=Decimal("5.00"),
            paid_at=NOW - timedelta(days=1),
        )
        # A second lead (different contact) that never bought.
        conv2 = enquiry(
            account, NOW - timedelta(days=3), reply_after=timedelta(minutes=1)
        )
        lead2 = Lead.objects.create(
            account=account, contact=conv2.contact, conversation=conv2
        )
        Lead.objects.filter(pk=lead2.pk).update(created_at=NOW - timedelta(days=2))

        period = reporting.Period(NOW - timedelta(days=4), NOW)
        s = reporting.sales(account, period)
        assert s["leads"] == 2
        assert s["leads_bought"] == 1
        assert s["lead_to_sale_pct"] == 50

    def test_per_workflow_sales(self, account):
        from apps.automation.models import Workflow, WorkflowRun
        from apps.commerce.models import Order

        workflow = Workflow.objects.create(
            account=account, name="Greeter", slug="greeter", status="published"
        )
        run = WorkflowRun.objects.create(workflow=workflow, contact=_contact(account))
        conv = enquiry(
            account, NOW - timedelta(days=1), reply_after=timedelta(minutes=1)
        )
        order = Order.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            status=Order.Status.PAID,
            currency="USD",
            total=Decimal("15.00"),
            paid_at=NOW - timedelta(hours=1),
        )
        ConversationAttribution.objects.create(
            account=account,
            conversation=conv,
            channel="whatsapp",
            order=order,
            workflow_run=run,
            method=ConversationAttribution.Method.RECENT_CONVERSATION,
        )
        period = reporting.Period(NOW - timedelta(days=2), NOW)
        rows = reporting.workflow_sales(account, period)
        assert len(rows) == 1
        assert rows[0]["slug"] == "greeter"
        assert rows[0]["paid_orders"] == 1
        assert rows[0]["revenue"] == [{"currency": "USD", "total": Decimal("15.00")}]


@pytest.mark.django_db
class TestAtRisk:
    def test_zero_rows_are_hidden(self, account):
        result = reporting.at_risk(account, NOW)
        assert result["counts"]["missed"] == 0
        keys = {a["key"] for a in result["actions"]}
        assert "missed" not in keys
        assert "waiting" not in keys

    def test_missed_and_followups_due_are_actions(self, account):
        conv = enquiry(account, NOW - timedelta(days=2))
        FollowUp.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            due_at=NOW - timedelta(hours=1),
        )
        result = reporting.at_risk(account, NOW)
        assert result["counts"]["missed"] == 1
        assert result["counts"]["followups_due"] == 1
        keys = {a["key"] for a in result["actions"]}
        assert "missed" in keys
        assert "followups_due" in keys

    def test_dashboard_and_insights_share_the_same_counts(self, account):
        from apps.accounts.views import _work_queue

        enquiry(account, NOW - timedelta(days=2))
        queue = _work_queue(account)
        risk = reporting.at_risk(account, NOW)
        assert queue["missed_count"] == risk["counts"]["missed"]
        assert queue["customers_waiting_count"] == risk["counts"]["waiting"]
        assert queue["followups_due_count"] == risk["counts"]["followups_due"]


@pytest.mark.django_db
class TestProofBar:
    def test_zero_clauses_are_left_out(self, account):
        week = reporting.Period(NOW - timedelta(days=7), NOW)
        result = reporting.proof(account, week, now=NOW)
        assert result["sentence_parts"] == []
        assert result["active"] is False

    def test_replies_recoveries_and_sales_all_appear(self, account):
        from apps.commerce.models import Order

        week_start = NOW - timedelta(days=6)
        week = reporting.Period(week_start, NOW)
        enquiry(
            account, week_start + timedelta(hours=1), reply_after=timedelta(minutes=2)
        )
        conv = enquiry(account, week_start + timedelta(hours=2))
        reminder = FollowUp.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            due_at=week_start + timedelta(days=1, hours=1),
            source=FollowUp.Source.MISSED,
        )
        FollowUp.objects.filter(pk=reminder.pk).update(
            created_at=week_start + timedelta(days=1, hours=1)
        )
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="Sorry!",
            status="sent",
            timestamp=week_start + timedelta(days=1, hours=2),
        )
        paid_conv = enquiry(
            account, week_start + timedelta(hours=3), reply_after=timedelta(minutes=1)
        )
        Order.objects.create(
            account=account,
            contact=paid_conv.contact,
            conversation=paid_conv,
            status=Order.Status.PAID,
            currency="USD",
            total=Decimal("20.00"),
            paid_at=week_start + timedelta(hours=4),
        )
        result = reporting.proof(account, week, now=NOW)
        assert result["active"] is True
        joined = " ".join(result["sentence_parts"])
        assert "got a reply" in joined
        assert "picked back up" in joined
        assert "paid order" in joined


@pytest.mark.django_db
class TestTeamAndSettings:
    def test_team_uses_median_not_mean(self, account):
        user = User.objects.create_user("agent", "agent@example.com", "pw")
        Membership.objects.create(
            user=user, account=account, role=Membership.Role.MEMBER
        )
        start = NOW - timedelta(hours=5)
        for minutes in (1, 2, 100):
            enquiry(
                account,
                start,
                reply_after=timedelta(minutes=minutes),
                assigned_to=user,
                phone=None,
            )
        period = reporting.Period(start - timedelta(hours=1), NOW)
        rows = reporting.team(account, period)
        assert len(rows) == 1
        assert rows[0]["median_first_reply_seconds"] == 120

    def test_minutes_per_reply_default_and_override(self, account):
        from apps.conversations.models import InsightSettings

        assert reporting.minutes_per_reply(account) == 2
        InsightSettings.objects.create(account=account, minutes_per_reply=5)
        assert reporting.minutes_per_reply(account) == 5


class TestDuration:
    def test_formats(self):
        assert reporting.duration(None) == "—"
        assert reporting.duration(45) == "45s"
        assert reporting.duration(134) == "2m 14s"
        assert reporting.duration(3 * 3600 + 5 * 60) == "3h 5m"
        assert reporting.duration(2 * 86400 + 4 * 3600) == "2d 4h"
