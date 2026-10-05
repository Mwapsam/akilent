"""The weekly owner report: content, gating and the once-per-(account, week) guard. See
apps.conversations.weekly_report and the send_weekly_reports task."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.contacts.models import Contact
from apps.conversations import reporting, weekly_report
from apps.conversations.models import (
    Conversation,
    Event,
    FollowUp,
    InsightSettings,
    Message,
)
from apps.conversations.snapshots import SETTLE
from apps.conversations.tasks import send_weekly_reports
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


def _owner(account, email="owner@example.com"):
    user = User.objects.create_user("owner", email, "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user


def _member(account):
    user = User.objects.create_user("staff", "staff@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.MEMBER)
    return user


def _connect(account, days_ago):
    number = WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id=f"PN{account.pk}",
        waba_id="W",
        access_token="t",
        is_active=True,
    )
    at = timezone.now() - timedelta(days=days_ago)
    WhatsAppBusinessNumber.objects.filter(pk=number.pk).update(created_at=at)
    return at


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


@pytest.mark.django_db
class TestReportWeek:
    def test_none_before_the_week_has_settled(self):
        now = timezone.now()
        week = reporting.week_of(now).previous()
        assert weekly_report.report_week(week.end) is None  # 0 days of settle time

    def test_the_previous_week_once_settled(self):
        now = timezone.now()
        expected = reporting.week_of(now).previous()
        assert weekly_report.report_week(expected.end + SETTLE) == expected


@pytest.mark.django_db
class TestBuildEmail:
    def test_none_when_nothing_happened(self, account):
        week = reporting.week_of(timezone.now()).previous()
        assert weekly_report.build_email(account, week, full=False) is None

    def test_content_includes_the_proof_sentence_and_the_open_link(self, account):
        week = reporting.week_of(timezone.now()).previous()
        _enquiry(
            account, week.start + timedelta(hours=1), reply_after=timedelta(minutes=3)
        )
        email = weekly_report.build_email(account, week, full=False)
        assert email is not None
        assert "got a reply" in email["subject"]
        assert "Median first reply" in email["text_body"]
        assert "/insights/" in email["text_body"]

    def test_recovery_rate_shown_only_when_something_was_missed(self, account):
        week = reporting.week_of(timezone.now()).previous()
        conv = _enquiry(account, week.start + timedelta(hours=1))
        reminder = FollowUp.objects.create(
            account=account,
            contact=conv.contact,
            conversation=conv,
            source=FollowUp.Source.MISSED,
            due_at=week.start + timedelta(hours=2),
        )
        FollowUp.objects.filter(pk=reminder.pk).update(
            created_at=week.start + timedelta(hours=2)
        )
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="sorry, here",
            status="sent",
            timestamp=week.start + timedelta(hours=3),
        )
        email = weekly_report.build_email(account, week, full=False)
        assert "Recovery rate" in email["text_body"]

    def test_opportunities_at_risk_are_included(self, account):
        week = reporting.week_of(timezone.now()).previous()
        _enquiry(
            account, week.start + timedelta(hours=1), reply_after=timedelta(minutes=1)
        )
        due_conv = _enquiry(account, week.start + timedelta(hours=2))
        FollowUp.objects.create(
            account=account,
            contact=due_conv.contact,
            conversation=due_conv,
            source=FollowUp.Source.MANUAL,
            due_at=week.end - timedelta(hours=1),
        )
        email = weekly_report.build_email(account, week, full=False)
        assert "Opportunities at risk" in email["text_body"]
        assert "follow-up" in email["text_body"]

    def test_team_table_only_appears_when_full(self, account):
        week = reporting.week_of(timezone.now()).previous()
        user = _owner(account)
        contact = Contact.objects.create(account=account, phone="+260971111111")
        conv = Conversation.objects.create(
            account=account, contact=contact, channel="whatsapp", assigned_to=user
        )
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=IN,
            body="hi",
            timestamp=week.start + timedelta(hours=1),
        )
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="ok",
            status="sent",
            timestamp=week.start + timedelta(hours=1, minutes=2),
        )
        short = weekly_report.build_email(account, week, full=False)
        full = weekly_report.build_email(account, week, full=True)
        assert "Team:" not in short["text_body"]
        assert "Team:" in full["text_body"]


def _settled_now():
    """A "now" that is always well past the 1-day SETTLE window for the previous week.

    report_week(now) only returns non-None when now >= last_monday_midnight + 1 day.
    Running tests on Monday mornings fails that check, so we advance to Wednesday.
    """
    now = timezone.now()
    days_to_wednesday = (2 - now.weekday()) % 7 or 7  # 2 = Wednesday; never 0
    return now + timedelta(days=days_to_wednesday)


@pytest.mark.django_db
class TestSendWeeklyReportsTask:
    @pytest.fixture(autouse=True)
    def _freeze_to_wednesday(self, monkeypatch):
        settled = _settled_now()
        monkeypatch.setattr("django.utils.timezone.now", lambda: settled)

    def _settled_week(self):
        # timezone.now() is mocked by _freeze_to_wednesday to a settled Wednesday
        return reporting.week_of(timezone.now()).previous()

    def test_sends_to_owners_only_and_records_the_event(self, account):
        _connect(account, days_ago=60)
        owner = _owner(account)
        _member(account)
        week = self._settled_week()
        _enquiry(
            account, week.start + timedelta(hours=1), reply_after=timedelta(minutes=1)
        )

        sent = send_weekly_reports()

        assert sent == 1
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [owner.email]
        assert Event.objects.filter(
            account=account,
            source="weekly_report",
            source_event_id=week.start.date().isoformat(),
        ).exists()

    def test_is_not_sent_twice_for_the_same_week(self, account):
        _connect(account, days_ago=60)
        _owner(account)
        week = self._settled_week()
        _enquiry(
            account, week.start + timedelta(hours=1), reply_after=timedelta(minutes=1)
        )

        send_weekly_reports()
        mail.outbox.clear()
        again = send_weekly_reports()

        assert again == 0
        assert len(mail.outbox) == 0

    def test_opt_out_is_respected(self, account):
        _connect(account, days_ago=60)
        _owner(account)
        InsightSettings.objects.create(account=account, weekly_report=False)
        week = self._settled_week()
        _enquiry(
            account, week.start + timedelta(hours=1), reply_after=timedelta(minutes=1)
        )

        assert send_weekly_reports() == 0
        assert len(mail.outbox) == 0

    def test_inactive_business_is_not_emailed(self, account):
        _connect(account, days_ago=60)
        _owner(account)

        assert send_weekly_reports() == 0
        assert len(mail.outbox) == 0

    def test_free_plan_gets_the_short_report_without_a_team_table(self, account):
        _connect(account, days_ago=60)
        owner = _owner(account)
        contact = Contact.objects.create(account=account, phone="+260971111111")
        conv = Conversation.objects.create(
            account=account, contact=contact, channel="whatsapp", assigned_to=owner
        )
        week = self._settled_week()
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=IN,
            body="hi",
            timestamp=week.start + timedelta(hours=1),
        )
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="ok",
            status="sent",
            timestamp=week.start + timedelta(hours=1, minutes=2),
        )

        send_weekly_reports()

        assert "Team:" not in mail.outbox[0].body

    def test_plan_with_insights_gets_the_full_report(self, account):
        plan = Plan.objects.create(
            slug="biz-report",
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
        _connect(account, days_ago=60)
        owner = _owner(account)
        contact = Contact.objects.create(account=account, phone="+260971111111")
        conv = Conversation.objects.create(
            account=account, contact=contact, channel="whatsapp", assigned_to=owner
        )
        week = self._settled_week()
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=IN,
            body="hi",
            timestamp=week.start + timedelta(hours=1),
        )
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="ok",
            status="sent",
            timestamp=week.start + timedelta(hours=1, minutes=2),
        )

        send_weekly_reports()

        assert "Team:" in mail.outbox[0].body


@pytest.mark.django_db
class TestReportSettingsView:
    def test_owner_can_turn_the_weekly_report_off(self, client, account):
        owner = _owner(account)
        client.force_login(owner)
        resp = client.post("/insights/report-settings/", {})
        assert resp.status_code == 302
        assert InsightSettings.objects.get(account=account).weekly_report is False

    def test_member_cannot_change_it(self, client, account):
        client.force_login(_member(account))
        client.post("/insights/report-settings/", {"weekly_report": "on"})
        assert not InsightSettings.objects.filter(account=account).exists()
