"""What optional tools a brand-new account starts with.

Orders and payments are out of the near-term product: day one is about
conversations, so a new business never meets an Orders tab it has no use for.
An account that already exists keeps whatever it has — nothing is taken away.
"""
import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.billing.api import usable
from apps.billing.models import Plan

SIGNUP_URL = "/signup/"
PW = "Sup3r-secret-pw"


def _payload(**overrides):
    data = {
        "email": "new@example.com",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "phone": "+260 900 000 000",
        "company_name": "New Co",
        "address_line1": "1 Main St",
        "city": "Lusaka",
        "country": "Zambia",
        "selected_services": Account.Services.BOTH,
        "plan": Plan.TRIAL,
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


@pytest.mark.django_db
def test_a_new_account_starts_without_orders(client, trial_plan):
    client.post(SIGNUP_URL, _payload())
    account = Account.objects.get(company_name="New Co")
    assert usable(account, "orders") is False


@pytest.mark.django_db
def test_a_new_account_still_tracks_interested_customers(client, trial_plan):
    """Opportunities are the point of the product, not an optional extra."""
    client.post(SIGNUP_URL, _payload())
    account = Account.objects.get(company_name="New Co")
    assert usable(account, "sales") is True


@pytest.mark.django_db
def test_an_existing_account_keeps_orders(db):
    """An account created before this change has no stored switch, and
    "no switch = on" must keep meaning on — we don't silently remove a
    feature someone is already using."""
    account = Account.objects.create(company_name="Old Co")
    assert usable(account, "orders") is True


@pytest.mark.django_db
def test_a_new_account_can_turn_orders_back_on(client, trial_plan):
    client.post(SIGNUP_URL, _payload())
    account = Account.objects.get(company_name="New Co")
    user = User.objects.get(email="new@example.com")
    assert Membership.objects.filter(user=user, account=account).exists()

    resp = client.post("/settings/tools/", {"sales": "on", "orders": "on"})
    assert resp.status_code == 302
    assert usable(account, "orders") is True
