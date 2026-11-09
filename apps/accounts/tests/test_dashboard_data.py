"""Dashboard data fixes (2026-11-09 review, step 1): the plan shows only when it needs
something and never "Active · renews" on a past date; the Inbox badge counts what "Customers
waiting" counts; "Recent activity" was email sends and now says so."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing import api as billing_api
from apps.billing.models import Plan, Subscription
from apps.contacts.models import Contact
from apps.conversations import followups
from apps.conversations.models import Conversation, Message

NOW = timezone.now()


@pytest.fixture
def owner(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


def _sub(account, **kw):
    plan = Plan.objects.filter(price_monthly__gt=0).first() or Plan.objects.create(
        name="Business", slug="business-t", price_monthly=10
    )
    Subscription.objects.filter(account=account).delete()
    return Subscription.objects.create(
        account=account,
        plan=plan,
        current_period_start=NOW - timedelta(days=30),
        **kw,
    )


def test_a_renewal_due_in_the_past_is_never_shown_as_active(owner):
    _, account, _ = owner
    sub = _sub(
        account, status=Subscription.ACTIVE, current_period_end=NOW - timedelta(days=2)
    )
    notice = billing_api.home_notice(sub, NOW)
    assert notice["tone"] == "warning"
    assert "hasn't gone through yet" in notice["text"]


def test_a_plan_with_nothing_to_do_stays_off_home(owner):
    _, account, _ = owner
    sub = _sub(
        account, status=Subscription.ACTIVE, current_period_end=NOW + timedelta(days=20)
    )
    assert billing_api.home_notice(sub, NOW) is None
    sub.current_period_end = NOW + timedelta(days=3)
    assert "renews" in billing_api.home_notice(sub, NOW)["text"]


def test_past_due_trial_and_no_plan_need_action(owner):
    _, account, _ = owner
    past_due = _sub(account, status=Subscription.PAST_DUE, current_period_end=NOW)
    assert billing_api.home_notice(past_due, NOW)["action"] == "Pay now"
    trial = _sub(
        account, status=Subscription.TRIALING, trial_ends_at=NOW + timedelta(days=2)
    )
    assert "trial ends" in billing_api.home_notice(trial, NOW)["text"]
    assert billing_api.home_notice(None, NOW)["action"] == "Choose a plan"


def test_home_shows_the_overdue_line_not_the_plan_block(owner):
    client, account, _ = owner
    _sub(
        account, status=Subscription.ACTIVE, current_period_end=NOW - timedelta(days=2)
    )
    html = client.get("/dashboard/panels/").content.decode()
    assert "data-plan-notice" in html and "gone through yet" in html
    assert "renews" not in html and 'id="dash-plan"' not in html


def _waiting(account, **kw):
    contact = Contact.objects.create(
        account=account, phone=f"+2609{Contact.objects.count():08d}"
    )
    conv = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp", **kw
    )
    Message.objects.create(
        account=account,
        conversation=conv,
        direction=Message.Direction.INBOUND,
        body="Hi",
        timestamp=NOW - timedelta(minutes=5),
    )
    return conv


def test_badge_number_is_customers_waiting_and_a_follow_up_is_a_dot(owner, monkeypatch):
    client, account, _ = owner
    monkeypatch.setattr(followups, "due_for", lambda user, account: 1)
    # Nothing waiting, one follow-up due: a dot, no number (it used to read "1").
    html = client.get("/inbox/badge/").content.decode()
    assert "inbox-tab-count--dot" in html and "></span>" in html
    assert 'data-due="1"' in html and "1 follow-up due" in html
    _waiting(account)
    html = client.get("/inbox/badge/").content.decode()
    assert ">1<" in html and "inbox-tab-count--due" in html
    assert "1 customer waiting for a reply and 1 follow-up due" in html
