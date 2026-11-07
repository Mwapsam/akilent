"""UI quality programme, Phase 5: the billing pages say exactly what the records say.

- The plan cards worked the price out in the browser and rounded it to whole units, so a
  yearly price checkout charges to the cent (288.84) read 289.
- After a bank transfer was sent in, the page still said "Payment required" with a
  "Complete payment" button: paying again a second way would pay twice.
- The pending-payment page said "Confirming your payment" even for a payment it couldn't
  find, and went quiet when it stopped checking.
- The usage warning promised held messages are "never lost"; campaign messages that wait
  past WHATSAPP_CAMPAIGN_HOLD_MAX_HOURS are dropped.
"""

import json
import re
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing import payments
from apps.billing.models import ManualPaymentRequest, Plan, Subscription
from apps.billing.money import format_price
from apps.billing.pricing import monthly_equivalent, price_for


@pytest.fixture
def owner_client(client, db):
    user = User.objects.create_user("payer", "payer@example.com", "pw")
    account = Account.objects.create(company_name="Pay Co")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


@pytest.fixture
def plan(db):
    return Plan.objects.create(
        slug="growth", name="Growth", price_monthly=Decimal("29.00")
    )


def _card_prices(body: str) -> dict:
    raw = re.search(r"x-data='\{ prices: (\{[^']*\}) \}'", body).group(1)
    return json.loads(raw.replace("&quot;", '"'))


@pytest.mark.django_db
def test_card_prices_are_what_checkout_charges(owner_client, plan):
    client, _ = owner_client
    prices = _card_prices(client.get("/billing/plans/").content.decode())
    for period, _label in Subscription.BILLING_PERIOD_CHOICES:
        assert prices[period]["total"] == format_price(price_for(plan, period))
        assert prices[period]["monthly"] == format_price(
            monthly_equivalent(plan, period)
        )
    assert "toFixed" not in client.get("/billing/plans/").content.decode()


@pytest.mark.django_db
def test_a_transfer_in_review_is_not_asked_for_again(owner_client, plan):
    client, account = owner_client
    Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.INCOMPLETE,
        current_period_start=timezone.now(),
    )
    payment = payments.create_payment(account, plan, "monthly", provider="manual")
    ManualPaymentRequest.objects.create(
        account=account,
        plan=plan,
        billing_period="monthly",
        payment=payment,
        reference="TX-123",
    )
    body = client.get("/billing/plans/").content.decode()
    assert "Payment in review" in body
    assert "TX-123" in body
    assert "You don't need to pay again" in body
    assert "Complete payment" not in body


@pytest.mark.django_db
def test_an_unknown_payment_is_not_called_confirming(owner_client):
    client, _ = owner_client
    body = client.get("/billing/return/manual/?reference=nope").content.decode()
    assert "We couldn't find this payment" in body
    assert "Confirming your payment" not in body
    assert "payment-status" not in body and "/status/" not in body


@pytest.mark.django_db
def test_held_campaign_messages_are_not_promised_forever(owner_client):
    from django.template.loader import render_to_string

    warning = {
        "key": "whatsapp_marketing_msgs",
        "name": "WhatsApp marketing messages",
        "unit": "messages",
        "percent": 100,
        "limit": 100,
        "used": 100,
        "over_limit": "hold",
        "resets": None,
    }
    with override_settings(WHATSAPP_CAMPAIGN_HOLD_MAX_HOURS=48):
        html = render_to_string(
            "billing/_usage_warnings.html", {"usage_warnings": [warning]}
        )
    assert "not lost" not in html
    assert "more than 48 hours" in html
    # Other held limits (email) don't mention campaigns.
    warning["key"] = "emails_month"
    html = render_to_string(
        "billing/_usage_warnings.html", {"usage_warnings": [warning]}
    )
    assert "Campaign messages" not in html
