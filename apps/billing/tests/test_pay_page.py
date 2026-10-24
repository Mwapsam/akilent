"""billing:pay — where a paid signup goes to pay. It creates nothing, leaves only by links
(form-action would block a redirect to the provider after the signup POST), and moves on to
checkout by itself once, straight after signup, when there's one way to pay."""

import re
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Membership
from apps.billing import credit
from apps.billing.models import BillingPayment, PaymentMethod, Subscription
from apps.billing.views import PAY_AUTOSTART
from apps.core.models import SiteSettings

PAY_URL = "/billing/pay/"


@pytest.fixture
def owner(db, account):
    user = User.objects.create_user("payowner", "owner@example.com", "pw12345!")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user


@pytest.fixture
def unpaid(db, account, plan):
    return Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.INCOMPLETE,
        billing_period=Subscription.ANNUALLY,
        current_period_start=timezone.now(),
    )


@pytest.fixture
def payments_on(db):
    site = SiteSettings.load()
    site.payments_enabled = True
    site.save()


@pytest.fixture
def no_provider(db):
    site = SiteSettings.load()
    site.billing_provider = ""
    site.save()


@pytest.fixture
def manual_on(db):
    PaymentMethod.objects.update_or_create(
        code="manual", defaults={"name": "Bank transfer", "is_enabled": True}
    )


def _get(client, owner, *, autostart=False):
    client.force_login(owner)
    if autostart:
        session = client.session
        session[PAY_AUTOSTART] = True
        session.save()
    return client.get(PAY_URL)


def _links(resp):
    return re.findall(r'href="(/billing/checkout/[^"]*)"', resp.content.decode())


def test_shows_what_checkout_will_charge_in_the_billing_currency(
    client, owner, unpaid, payments_on, fake_provider, settings
):
    settings.BILLING_CURRENCY = "ZMW"
    resp = _get(client, owner)
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "Starter" in html and "Annually" in html
    assert "K193.80 / year" in html  # 19 x 12 x 0.85
    assert (
        "$" not in html.split('aria-label="Payment summary"')[1].split("</section>")[0]
    )
    assert _links(resp) == [
        "/billing/checkout/?plan=starter&amp;period=annually&amp;method=fake"
    ]
    # Nothing is created until a way to pay is picked.
    assert not BillingPayment.objects.exists()


def test_never_posts_a_form_to_checkout(
    client, owner, unpaid, payments_on, fake_provider, manual_on
):
    html = _get(client, owner).content.decode()
    assert not re.search(r"<form[^>]*checkout", html)


def test_offers_bank_transfer_beside_the_card_provider(
    client, owner, unpaid, payments_on, fake_provider, manual_on
):
    resp = _get(client, owner, autostart=True)
    assert [link.rsplit("=", 1)[1] for link in _links(resp)] == ["fake", "manual"]
    # Two ways to pay: the customer chooses, nothing starts by itself.
    assert "location.assign" not in resp.content.decode()


def test_bank_transfer_alone_is_enough(
    client, owner, unpaid, payments_on, no_provider, manual_on
):
    resp = _get(client, owner)
    assert [link.rsplit("=", 1)[1] for link in _links(resp)] == ["manual"]


def test_without_online_payment_it_says_so(
    client, owner, unpaid, fake_provider, manual_on
):
    site = SiteSettings.load()
    site.payments_enabled = False
    site.save()
    resp = _get(client, owner)
    assert resp.status_code == 200
    assert _links(resp) == []
    assert b"Online payment isn't available right now" in resp.content


def test_credit_comes_off_before_payment(
    client, owner, account, unpaid, payments_on, fake_provider
):
    credit.grant(account, Decimal("50"))
    html = _get(client, owner).content.decode()
    assert "\u2212$50.00" in html
    assert "$143.80" in html  # 193.80 - 50


def test_credit_that_covers_it_all_pays_without_a_provider(
    client, owner, account, unpaid, payments_on, fake_provider, manual_on
):
    credit.grant(account, Decimal("500"))
    resp = _get(client, owner)
    assert b"Pay with account credit" in resp.content
    assert len(_links(resp)) == 1


def test_moves_on_to_checkout_once_straight_after_signup(
    client, owner, unpaid, payments_on, fake_provider
):
    first = _get(client, owner, autostart=True).content.decode()
    assert 'window.location.assign("/billing/checkout/' in first
    # Back from the provider, or a reload: the page waits for a click.
    again = client.get(PAY_URL).content.decode()
    assert "location.assign" not in again


@pytest.mark.parametrize(
    "status", [Subscription.ACTIVE, Subscription.TRIALING, Subscription.PAST_DUE]
)
def test_only_an_unpaid_new_plan_is_paid_here(
    client, owner, unpaid, payments_on, fake_provider, status
):
    unpaid.status = status
    unpaid.save()
    resp = _get(client, owner)
    assert resp.status_code == 302 and resp.url == "/billing/plans/"


def test_the_query_string_cannot_pick_another_plan(
    client, owner, unpaid, payments_on, fake_provider
):
    client.force_login(owner)
    resp = client.get(PAY_URL + "?plan=business&period=monthly")
    assert _links(resp) == [
        "/billing/checkout/?plan=starter&amp;period=annually&amp;method=fake"
    ]


def test_plans_page_finishes_an_unpaid_signup_here(
    client, owner, unpaid, payments_on, fake_provider
):
    client.force_login(owner)
    html = client.get("/billing/plans/").content.decode()
    assert f'href="{PAY_URL}" class="btn btn-primary btn-sm">Complete payment' in html
