"""The customer's return from a provider's hosted page (/billing/return/<provider>/, and the old
/billing/stripe/success/). The redirect is never proof of payment, and a reference only works
for the caller's own account."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing import payments
from apps.billing.models import BillingPayment, Plan, Subscription

RETRIEVE = "apps.billing.providers.stripe.stripe_sdk.checkout.Session.retrieve"


@pytest.fixture(autouse=True)
def stripe_keys(settings):
    settings.STRIPE_SECRET_KEY = "sk_test"
    settings.STRIPE_WEBHOOK_SECRET = "whsec_test"


@pytest.fixture
def plan(db):
    return Plan.objects.create(
        slug="starter", name="Starter", price_monthly=Decimal("19")
    )


def _business(name, plan):
    account = Account.objects.create(company_name=name)
    Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.INCOMPLETE,
        current_period_start=timezone.now(),
    )
    user = User.objects.create_user(username=name.lower(), password="pw12345!")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account, user


def _session(payment, *, status="paid"):
    return {
        "id": "cs_1",
        "mode": "payment",
        "payment_status": status,
        "customer": "cus_1",
        "client_reference_id": payment.reference,
        "currency": "usd",
        "amount_total": int(payment.amount_due * 100),
        "payment_intent": {"id": "pi_1", "payment_method": {"id": "pm_1", "card": {}}},
    }


def _return(client, payment, session):
    with patch(RETRIEVE, return_value=session):
        return client.get(
            f"/billing/return/stripe/?ref={payment.reference}&session_id=cs_1"
        )


@pytest.mark.django_db
def test_login_required(client, plan):
    resp = client.get("/billing/stripe/success/?ref=pay_x&session_id=cs_1")
    assert resp.status_code == 302 and "/login" in resp["Location"].lower()


@pytest.mark.django_db
def test_matching_account_activates_and_redirects(client, plan):
    account, user = _business("Alpha", plan)
    payment = payments.create_payment(account, plan, "monthly", provider="stripe")
    client.force_login(user)

    resp = _return(client, payment, _session(payment))

    assert resp.status_code == 302
    account.subscription.refresh_from_db()
    assert account.subscription.status == Subscription.ACTIVE


@pytest.mark.django_db
def test_reference_for_different_account_is_not_activated(client, plan):
    account_a, _ = _business("Alpha", plan)
    _, user_b = _business("Beta", plan)
    payment = payments.create_payment(account_a, plan, "monthly", provider="stripe")
    client.force_login(user_b)

    resp = _return(client, payment, _session(payment))

    assert (
        resp.status_code == 200
    )  # the same "still confirming" page as an unpaid session
    account_a.subscription.refresh_from_db()
    assert account_a.subscription.status == Subscription.INCOMPLETE
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING


@pytest.mark.django_db
def test_unpaid_session_does_not_activate(client, plan):
    account, user = _business("Alpha", plan)
    payment = payments.create_payment(account, plan, "monthly", provider="stripe")
    client.force_login(user)

    resp = _return(client, payment, _session(payment, status="unpaid"))

    assert resp.status_code == 200
    account.subscription.refresh_from_db()
    assert account.subscription.status == Subscription.INCOMPLETE


@pytest.mark.django_db
def test_missing_reference_shows_pending_page(client, plan):
    _, user = _business("Alpha", plan)
    client.force_login(user)
    resp = client.get("/billing/stripe/success/")
    assert resp.status_code == 200
