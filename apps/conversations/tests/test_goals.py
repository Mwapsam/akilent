"""Goals: "Are we on track?" See apps.conversations.reporting.goal_progress and the
create/update/delete views in apps.conversations.insights_views."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.commerce.models import Order
from apps.contacts.models import Contact
from apps.conversations import reporting
from apps.conversations.models import Conversation, InsightGoal, Message

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


def _owner(account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user


def _member(account):
    user = User.objects.create_user("staff", "staff@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.MEMBER)
    return user


def _enquiry(account, at, reply_after=None):
    contact = Contact.objects.create(
        account=account, phone=f"+26097{Contact.objects.count():07d}"
    )
    conv = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    Message.objects.create(
        account=account, conversation=conv, direction=IN, body="hi", timestamp=at
    )
    if reply_after is not None:
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="ok",
            status="sent",
            timestamp=at + reply_after,
        )
    return conv


def _first_of_month(now):
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@pytest.mark.django_db
class TestMonthHelpers:
    def test_month_elapsed_share_at_month_start(self):
        now = timezone.now().replace(day=1, hour=6)
        share = reporting.month_elapsed_share(now)
        assert 0 < share <= 1 / 28  # day 1 of at most a 31-day month

    def test_month_elapsed_share_on_the_last_day_is_one(self):
        import calendar

        now = timezone.now()
        _, days_in_month = calendar.monthrange(now.year, now.month)
        last_day = now.replace(day=days_in_month, hour=12)
        assert reporting.month_elapsed_share(last_day) == 1.0


@pytest.mark.django_db
class TestGoalProgress:
    def test_lower_is_better_compares_directly_not_paced(self, account):
        now = timezone.now()
        month_start = _first_of_month(now)
        _enquiry(
            account, month_start + timedelta(hours=1), reply_after=timedelta(minutes=3)
        )
        InsightGoal.objects.create(
            account=account,
            metric=InsightGoal.Metric.MEDIAN_FIRST_REPLY,
            target=Decimal("5"),
        )
        result = reporting.goal_progress(account, now=now)
        assert result["total"] == 1
        row = result["goals"][0]
        assert row["actual"] == pytest.approx(3.0)
        assert row["on_track"] is True  # 3 min actual <= 5 min target

    def test_lower_is_better_behind_when_actual_exceeds_target(self, account):
        now = timezone.now()
        month_start = _first_of_month(now)
        _enquiry(
            account, month_start + timedelta(hours=1), reply_after=timedelta(minutes=10)
        )
        InsightGoal.objects.create(
            account=account,
            metric=InsightGoal.Metric.MEDIAN_FIRST_REPLY,
            target=Decimal("5"),
        )
        row = reporting.goal_progress(account, now=now)["goals"][0]
        assert row["on_track"] is False

    def test_no_data_yet_is_unknown_not_behind(self, account):
        now = timezone.now()
        InsightGoal.objects.create(
            account=account,
            metric=InsightGoal.Metric.MEDIAN_FIRST_REPLY,
            target=Decimal("5"),
        )
        row = reporting.goal_progress(account, now=now)["goals"][0]
        assert row["actual"] is None
        assert row["on_track"] is None

    def test_paced_goal_on_track_only_if_progress_keeps_up_with_the_month(
        self, account
    ):
        # A day deep into a 30-day month: about 1/3 of the way through.
        now = timezone.now().replace(day=10, hour=12)
        month_start = _first_of_month(now)
        from apps.crm.models import Lead

        for _ in range(2):
            conv = _enquiry(account, month_start + timedelta(hours=1))
            lead = Lead.objects.create(
                account=account, contact=conv.contact, conversation=conv
            )
            Lead.objects.filter(pk=lead.pk).update(
                created_at=month_start + timedelta(hours=2)
            )
        InsightGoal.objects.create(
            account=account, metric=InsightGoal.Metric.LEADS, target=Decimal("20")
        )
        row = reporting.goal_progress(account, now=now)["goals"][0]
        assert row["actual"] == 2.0
        # 2/20 = 10% progress, well under ~33% of the month elapsed -> behind.
        assert row["on_track"] is False

    def test_paced_goal_on_track_when_ahead_of_the_month(self, account):
        now = timezone.now().replace(day=5)
        month_start = _first_of_month(now)
        from apps.crm.models import Lead

        for _ in range(15):
            conv = _enquiry(account, month_start + timedelta(hours=1))
            lead = Lead.objects.create(
                account=account, contact=conv.contact, conversation=conv
            )
            Lead.objects.filter(pk=lead.pk).update(
                created_at=month_start + timedelta(hours=2)
            )
        InsightGoal.objects.create(
            account=account, metric=InsightGoal.Metric.LEADS, target=Decimal("20")
        )
        row = reporting.goal_progress(account, now=now)["goals"][0]
        assert row["on_track"] is True

    def test_revenue_goal_is_scoped_to_its_own_currency(self, account):
        now = timezone.now()
        month_start = _first_of_month(now)
        conv = _enquiry(
            account, month_start + timedelta(hours=1), reply_after=timedelta(minutes=1)
        )
        Order.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            status=Order.Status.PAID,
            currency="USD",
            total=Decimal("500.00"),
            paid_at=month_start + timedelta(hours=2),
        )
        Order.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            status=Order.Status.PAID,
            currency="ZMW",
            total=Decimal("9000.00"),
            paid_at=month_start + timedelta(hours=3),
        )
        InsightGoal.objects.create(
            account=account,
            metric=InsightGoal.Metric.REVENUE,
            target=Decimal("1000"),
            currency="USD",
        )
        row = reporting.goal_progress(account, now=now)["goals"][0]
        assert row["actual"] == 500.0  # ZMW revenue is never folded into the USD goal

    def test_on_track_count_summarises_all_goals(self, account):
        now = timezone.now()
        month_start = _first_of_month(now)
        _enquiry(
            account, month_start + timedelta(hours=1), reply_after=timedelta(minutes=1)
        )
        InsightGoal.objects.create(
            account=account,
            metric=InsightGoal.Metric.MEDIAN_FIRST_REPLY,
            target=Decimal("5"),
        )
        InsightGoal.objects.create(
            account=account, metric=InsightGoal.Metric.LEADS, target=Decimal("100")
        )
        result = reporting.goal_progress(account, now=now)
        assert result["total"] == 2
        assert (
            result["on_track"] == 1
        )  # reply goal met; leads goal has no data (unknown)


@pytest.mark.django_db
class TestGoalUniqueness:
    def test_two_goals_for_the_same_metric_conflict(self, account):
        from django.db import IntegrityError

        InsightGoal.objects.create(
            account=account, metric=InsightGoal.Metric.LEADS, target=Decimal("10")
        )
        with pytest.raises(IntegrityError):
            InsightGoal.objects.create(
                account=account, metric=InsightGoal.Metric.LEADS, target=Decimal("20")
            )

    def test_revenue_goals_in_different_currencies_coexist(self, account):
        InsightGoal.objects.create(
            account=account,
            metric=InsightGoal.Metric.REVENUE,
            target=Decimal("1000"),
            currency="USD",
        )
        InsightGoal.objects.create(
            account=account,
            metric=InsightGoal.Metric.REVENUE,
            target=Decimal("5000"),
            currency="ZMW",
        )
        assert InsightGoal.objects.filter(account=account).count() == 2


@pytest.mark.django_db
class TestGoalViews:
    def test_owner_can_create_a_goal(self, client, account):
        client.force_login(_owner(account))
        resp = client.post(
            "/insights/goals/create/", {"metric": "leads", "target": "50"}
        )
        assert resp.status_code == 302
        assert InsightGoal.objects.filter(account=account, metric="leads").exists()

    def test_member_cannot_create_a_goal(self, client, account):
        client.force_login(_member(account))
        client.post("/insights/goals/create/", {"metric": "leads", "target": "50"})
        assert not InsightGoal.objects.filter(account=account).exists()

    def test_revenue_goal_requires_a_currency(self, client, account):
        client.force_login(_owner(account))
        client.post("/insights/goals/create/", {"metric": "revenue", "target": "100"})
        assert not InsightGoal.objects.filter(account=account).exists()

    def test_zero_or_negative_target_is_rejected(self, client, account):
        client.force_login(_owner(account))
        client.post("/insights/goals/create/", {"metric": "leads", "target": "0"})
        assert not InsightGoal.objects.filter(account=account).exists()

    def test_free_plan_is_capped_at_three_goals(self, client, account):
        client.force_login(_owner(account))
        for metric, target in [
            ("median_first_reply", "5"),
            ("answered_pct", "80"),
            ("leads", "10"),
        ]:
            client.post("/insights/goals/create/", {"metric": metric, "target": target})
        assert InsightGoal.objects.filter(account=account).count() == 3
        client.post("/insights/goals/create/", {"metric": "paid_orders", "target": "5"})
        assert InsightGoal.objects.filter(account=account).count() == 3

    def test_plan_with_insights_has_no_goal_cap(self, client, account):
        plan = Plan.objects.create(
            slug="biz-goals",
            name="Business",
            price_monthly=Decimal("99"),
            detailed_analytics=True,
        )
        Subscription.objects.create(
            account=account,
            plan=plan,
            status=Subscription.ACTIVE,
            current_period_start=timezone.now(),
        )
        client.force_login(_owner(account))
        for metric, target in [
            ("median_first_reply", "5"),
            ("answered_pct", "80"),
            ("leads", "10"),
            ("paid_orders", "5"),
        ]:
            client.post("/insights/goals/create/", {"metric": metric, "target": target})
        assert InsightGoal.objects.filter(account=account).count() == 4

    def test_owner_can_update_a_goals_target(self, client, account):
        client.force_login(_owner(account))
        goal = InsightGoal.objects.create(
            account=account, metric=InsightGoal.Metric.LEADS, target=Decimal("10")
        )
        client.post(f"/insights/goals/{goal.pk}/update/", {"target": "25"})
        goal.refresh_from_db()
        assert goal.target == Decimal("25.00")

    def test_owner_can_delete_a_goal(self, client, account):
        client.force_login(_owner(account))
        goal = InsightGoal.objects.create(
            account=account, metric=InsightGoal.Metric.LEADS, target=Decimal("10")
        )
        client.post(f"/insights/goals/{goal.pk}/delete/")
        assert not InsightGoal.objects.filter(pk=goal.pk).exists()

    def test_cannot_delete_another_accounts_goal(self, client, account):
        other = Account.objects.create(company_name="Other")
        goal = InsightGoal.objects.create(
            account=other, metric=InsightGoal.Metric.LEADS, target=Decimal("10")
        )
        client.force_login(_owner(account))
        client.post(f"/insights/goals/{goal.pk}/delete/")
        assert InsightGoal.objects.filter(pk=goal.pk).exists()

    def test_the_insights_page_shows_goal_status(self, client, account):
        client.force_login(_owner(account))
        now = timezone.now()
        _enquiry(
            account,
            _first_of_month(now) + timedelta(hours=1),
            reply_after=timedelta(minutes=1),
        )
        InsightGoal.objects.create(
            account=account,
            metric=InsightGoal.Metric.MEDIAN_FIRST_REPLY,
            target=Decimal("5"),
        )
        body = client.get("/insights/").content.decode()
        assert "on track" in body
        assert "Median first reply" in body
