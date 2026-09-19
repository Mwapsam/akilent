import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account
from apps.billing.models import Plan, Subscription

SIGNUP_URL = "/signup/"
PW = "Sup3r-secret-pw"


def _payload(**overrides):
    data = {
        "email": "paid@example.com",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "phone": "+260 900 000 000",
        "company_name": "Paid Co",
        "address_line1": "1 Main St",
        "city": "Lusaka",
        "country": "Zambia",
        "selected_services": Account.Services.EMAIL,
        "plan": Plan.STARTER,
        "password1": PW,
        "password2": PW,
    }
    data.update(overrides)
    return data


@pytest.fixture
def trial_plan(db):
    return Plan.objects.create(
        slug=Plan.TRIAL, name="Trial", price_monthly=0, trial_days=14,
        service_type=Plan.SERVICE_BOTH,
    )


@pytest.fixture
def paid_plan(db):
    return Plan.objects.create(
        slug=Plan.STARTER, name="Starter", price_monthly=19,
        service_type=Plan.SERVICE_EMAIL, trial_days=0,
    )


@pytest.fixture
def paid_plan_with_long_trial(db):
    """Deliberately carries a generous trial_days — must be ignored: a paid
    plan chosen at signup is billed immediately, with no grace period."""
    return Plan.objects.create(
        slug=Plan.STARTER, name="Starter", price_monthly=19,
        service_type=Plan.SERVICE_EMAIL, trial_days=365,
    )


@pytest.mark.django_db
def test_free_plan_signup_skips_checkout(client, trial_plan):
    resp = client.post(SIGNUP_URL, _payload(plan=Plan.TRIAL, selected_services=Account.Services.BOTH))
    assert resp.status_code == 302
    assert not resp.url.startswith("/billing/checkout/")

    account = Account.objects.get(company_name="Paid Co")
    subscription = Subscription.objects.get(account=account)
    assert subscription.status in (Subscription.TRIALING, Subscription.ACTIVE)


@pytest.mark.django_db
def test_paid_plan_signup_redirects_to_checkout(client, trial_plan, paid_plan):
    resp = client.post(SIGNUP_URL, _payload())
    assert resp.status_code == 302
    assert resp.url == "/billing/checkout/?plan=starter&period=monthly"

    account = Account.objects.get(company_name="Paid Co")
    subscription = Subscription.objects.get(account=account)
    assert subscription.status == Subscription.INCOMPLETE
    assert subscription.trial_ends_at is None

    # Logged in immediately even though payment isn't complete.
    user = User.objects.get(email="paid@example.com")
    assert client.session.get("_auth_user_id") == str(user.pk)


@pytest.mark.django_db
def test_paid_plan_with_trial_days_still_bills_immediately(client, trial_plan, paid_plan_with_long_trial):
    resp = client.post(SIGNUP_URL, _payload())
    assert resp.status_code == 302
    assert resp.url.startswith("/billing/checkout/")

    account = Account.objects.get(company_name="Paid Co")
    subscription = Subscription.objects.get(account=account)
    assert subscription.status == Subscription.INCOMPLETE
    assert subscription.trial_ends_at is None
