from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription

SUCCESS_URL = "/billing/stripe/success/"


def _session(*, account, plan_slug="starter", billing_period="monthly", paid=True, customer="cus_123"):
    return {
        "mode": "subscription",
        "payment_status": "paid" if paid else "open",
        "customer": customer,
        "subscription": "sub_123",
        "metadata": {
            "account_id": str(account.pk),
            "plan_slug": plan_slug,
            "billing_period": billing_period,
        },
    }


def _login_owner(client, account, *, username):
    """Log in a fresh user who owns (only) ``account`` — get_current_account
    resolves to it via the owner-membership fallback, no session pinning
    needed."""
    user = User.objects.create_user(username=username, email=username, password="pw12345!")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return user


@pytest.fixture
def paid_plan(db):
    return Plan.objects.create(
        slug="starter", name="Starter", price_monthly=Decimal("19.00"),
    )


@pytest.fixture
def account_a(db):
    return Account.objects.create(company_name="Account A")


@pytest.fixture
def account_b(db):
    return Account.objects.create(company_name="Account B")


@pytest.fixture
def incomplete_subscription_a(db, account_a, paid_plan):
    return Subscription.objects.create(
        account=account_a, plan=paid_plan, status=Subscription.INCOMPLETE,
        current_period_start=timezone.now(),
    )


@pytest.fixture
def incomplete_subscription_b(db, account_b, paid_plan):
    return Subscription.objects.create(
        account=account_b, plan=paid_plan, status=Subscription.INCOMPLETE,
        current_period_start=timezone.now(),
    )


@pytest.mark.django_db
def test_unauthenticated_request_is_redirected(client, incomplete_subscription_a):
    resp = client.get(f"{SUCCESS_URL}?session_id=cs_test_123")
    assert resp.status_code == 302
    assert "/login" in resp.url


@pytest.mark.django_db
def test_matching_account_activates_and_redirects(client, account_a, incomplete_subscription_a, settings):
    _login_owner(client, account_a, username="ownera@example.com")

    with patch("apps.billing.views.stripe.checkout.Session.retrieve") as retrieve:
        retrieve.return_value = _session(account=account_a)
        resp = client.get(f"{SUCCESS_URL}?session_id=cs_test_123")

    assert resp.status_code == 302
    incomplete_subscription_a.refresh_from_db()
    assert incomplete_subscription_a.status == Subscription.ACTIVE
    assert incomplete_subscription_a.stripe_subscription_id == "sub_123"


@pytest.mark.django_db
def test_session_for_different_account_is_not_activated(
    client, account_a, account_b, incomplete_subscription_a, incomplete_subscription_b
):
    _login_owner(client, account_a, username="ownera2@example.com")

    # A session whose metadata names account_b — must not activate account_a
    # (the logged-in caller) OR account_b (whose session this actually is)
    # just because someone with a valid login hit this URL.
    with patch("apps.billing.views.stripe.checkout.Session.retrieve") as retrieve:
        retrieve.return_value = _session(account=account_b)
        resp = client.get(f"{SUCCESS_URL}?session_id=cs_test_123")

    assert resp.status_code == 200  # generic pending page, not an error page
    incomplete_subscription_a.refresh_from_db()
    incomplete_subscription_b.refresh_from_db()
    assert incomplete_subscription_a.status == Subscription.INCOMPLETE
    assert incomplete_subscription_b.status == Subscription.INCOMPLETE


@pytest.mark.django_db
def test_unpaid_session_does_not_activate(client, account_a, incomplete_subscription_a):
    _login_owner(client, account_a, username="ownera3@example.com")

    with patch("apps.billing.views.stripe.checkout.Session.retrieve") as retrieve:
        retrieve.return_value = _session(account=account_a, paid=False)
        resp = client.get(f"{SUCCESS_URL}?session_id=cs_test_123")

    assert resp.status_code == 200
    incomplete_subscription_a.refresh_from_db()
    assert incomplete_subscription_a.status == Subscription.INCOMPLETE


@pytest.mark.django_db
def test_missing_session_id_shows_pending_page(client, account_a, incomplete_subscription_a):
    _login_owner(client, account_a, username="ownera4@example.com")

    resp = client.get(SUCCESS_URL)
    assert resp.status_code == 200
    incomplete_subscription_a.refresh_from_db()
    assert incomplete_subscription_a.status == Subscription.INCOMPLETE
