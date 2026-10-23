from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription

CANCEL_URL = "/billing/cancel/"


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Cancel Co")


@pytest.fixture
def active_subscription(db, account):
    plan = Plan.objects.create(
        slug="starter", name="Starter", price_monthly=Decimal("19")
    )
    return Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
        current_period_end=timezone.now() + timedelta(days=20),
    )


def _member(account, username, role):
    user = User.objects.create_user(username=username, password="pw12345!")
    Membership.objects.create(user=user, account=account, role=role)
    return user


@pytest.mark.django_db
def test_member_cannot_cancel(client, account, active_subscription):
    user = _member(account, "member1", Membership.Role.MEMBER)
    client.force_login(user)
    resp = client.post(CANCEL_URL)
    assert resp.status_code == 302
    active_subscription.refresh_from_db()
    assert active_subscription.status == Subscription.ACTIVE


@pytest.mark.django_db
def test_owner_can_cancel(client, account, active_subscription):
    user = _member(account, "owner1", Membership.Role.OWNER)
    client.force_login(user)
    resp = client.post(CANCEL_URL)
    assert resp.status_code == 302
    active_subscription.refresh_from_db()
    # Ends at the end of the paid period, not now.
    assert active_subscription.status == Subscription.ACTIVE
    assert active_subscription.cancel_at_period_end


@pytest.mark.django_db
def test_cancelled_subscription_ends_at_period_end(
    client, account, active_subscription
):
    from apps.billing import renewals

    user = _member(account, "owner4", Membership.Role.OWNER)
    client.force_login(user)
    client.post(CANCEL_URL)
    renewals.run(now=active_subscription.current_period_end + timedelta(minutes=1))
    active_subscription.refresh_from_db()
    assert active_subscription.status == Subscription.CANCELLED


@pytest.mark.django_db
def test_owner_can_resume_before_period_end(client, account, active_subscription):
    user = _member(account, "owner5", Membership.Role.OWNER)
    client.force_login(user)
    client.post(CANCEL_URL)
    client.post("/billing/resume/")
    active_subscription.refresh_from_db()
    assert not active_subscription.cancel_at_period_end


@pytest.mark.django_db
def test_owner_can_cancel_legacy_stripe_subscription(
    client, account, active_subscription, settings
):
    settings.STRIPE_SECRET_KEY = "sk_test"
    active_subscription.payment_method = "stripe"
    active_subscription.stripe_subscription_id = "sub_123"
    active_subscription.save(update_fields=["payment_method", "stripe_subscription_id"])

    user = _member(account, "owner2", Membership.Role.OWNER)
    client.force_login(user)
    with patch(
        "apps.billing.providers.stripe.stripe_sdk.Subscription.modify", return_value={}
    ) as modify:
        resp = client.post(CANCEL_URL)

    assert resp.status_code == 302
    assert modify.call_args.args[0] == "sub_123"
    assert modify.call_args.kwargs["cancel_at_period_end"] is True
    active_subscription.refresh_from_db()
    assert not active_subscription.stripe_subscription_id
    assert active_subscription.cancel_at_period_end


@pytest.mark.django_db
def test_ajax_cancel_returns_redirect_json(client, account, active_subscription):
    user = _member(account, "owner3", Membership.Role.OWNER)
    client.force_login(user)
    resp = client.post(CANCEL_URL, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    # The data-ajax form's fetch() needs JSON to navigate; a bare 302 is
    # followed silently and leaves the page stale.
    assert resp.status_code == 200
    assert resp.json() == {"redirect": "/billing/plans/"}
    active_subscription.refresh_from_db()
    assert active_subscription.cancel_at_period_end
