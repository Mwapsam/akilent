"""billing:pay — where signup sends a paid plan, so the hop to the provider is a plain link.

Checkout redirects to the provider's hosted page; reached straight from the signup POST,
that redirect is blocked by CSP form-action and the browser stays on the signup page.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan
from apps.core.models import SiteSettings


@pytest.fixture
def owner_client(client, db):
    user = User.objects.create_user("o", "o@example.com", "Sup3r-secret-pw")
    acc = Account.objects.create(company_name="Co")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, acc


@pytest.fixture
def starter(db):
    return Plan.objects.create(
        slug=Plan.STARTER,
        name="Starter",
        price_monthly=Decimal("19"),
        service_type="email",
    )


@pytest.mark.django_db
def test_pay_page_shows_the_term_price_and_links_to_checkout(
    owner_client, starter, monkeypatch
):
    client, _ = owner_client
    site = SiteSettings.load()
    site.payments_enabled = True
    site.save()
    monkeypatch.setattr(
        "apps.billing.views.checkout_options",
        lambda account=None: [{"code": "paypal", "name": "PayPal"}],
    )
    resp = client.get("/billing/pay/?plan=starter&period=annually")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "$193.80" in html  # 19 x 12 x 0.85
    assert (
        "/billing/checkout/?plan=starter&amp;period=annually&amp;method=paypal" in html
    )
    # A link, not a form: the hop to the provider must not follow a form submit.
    assert "<form" not in html.split('id="pay-summary"')[1].split("</main>")[0]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "query", ["plan=nope", "plan=starter&period=weekly", "plan=trial"]
)
def test_pay_page_sends_anything_else_to_plans(owner_client, starter, query):
    client, _ = owner_client
    Plan.objects.create(slug=Plan.TRIAL, name="Trial", price_monthly=0)
    resp = client.get("/billing/pay/?" + query)
    assert resp.status_code == 302 and resp.url == "/billing/plans/"


@pytest.mark.django_db
def test_pay_page_without_online_payment_says_so(owner_client, starter):
    client, _ = owner_client
    site = SiteSettings.load()
    site.payments_enabled = False
    site.save()
    resp = client.get("/billing/pay/?plan=starter&period=monthly")
    assert resp.status_code == 200
    assert b"Online payment isn" in resp.content
    assert b"/billing/checkout/" not in resp.content
