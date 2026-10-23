"""Operator console: account credit, the payment provider switch, and early-adopter grants."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership, PilotEnrollment
from apps.billing import credit
from apps.billing.models import CreditGrant, Plan, Subscription
from apps.core.models import AdminAction, SiteSettings


@pytest.fixture
def operator(db):
    return User.objects.create_superuser("root", "root@example.com", "pw")


@pytest.fixture
def business(db):
    owner = User.objects.create_user("owner", "owner@acme.test", "pw")
    account = Account.objects.create(company_name="Acme", slug="acme")
    Membership.objects.create(user=owner, account=account, role=Membership.Role.OWNER)
    plan = Plan.objects.create(
        slug="starter", name="Starter", price_monthly=Decimal("19")
    )
    Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )
    return account


@pytest.fixture
def op_client(client, operator):
    client.force_login(operator)
    return client


@pytest.mark.django_db
def test_billing_tab_renders_credit_and_payments(op_client, business):
    credit.grant(business, "150", reason=CreditGrant.EARLY_ADOPTER, note="wave 1")
    resp = op_client.get(f"/manage/businesses/{business.pk}/tab/billing/")
    assert resp.status_code == 200
    assert b"Account credit" in resp.content and b"$150.00" in resp.content


@pytest.mark.django_db
def test_operator_grants_and_revokes_credit_with_audit(op_client, business):
    op_client.post(
        f"/manage/businesses/{business.pk}/do/credit_grant/",
        {"amount": "150", "reason": "early_adopter", "days": "180", "note": "wave 1"},
    )
    g = CreditGrant.objects.get(account=business)
    assert g.amount == Decimal("150.00") and g.expires_at is not None
    assert AdminAction.objects.filter(action="business.credit.grant").exists()

    op_client.post(
        f"/manage/businesses/{business.pk}/do/credit_revoke/", {"grant": g.pk}
    )
    assert credit.balance(business) == 0
    assert AdminAction.objects.filter(action="business.credit.revoke").exists()


@pytest.mark.django_db
def test_owner_cannot_grant_themselves_credit(client, business):
    client.force_login(business.owner)
    client.post(f"/manage/businesses/{business.pk}/do/credit_grant/", {"amount": "999"})
    assert not CreditGrant.objects.exists()


@pytest.mark.django_db
def test_settings_page_lists_providers(op_client):
    resp = op_client.get("/manage/settings/")
    assert resp.status_code == 200
    assert b"Payment provider" in resp.content and b"Stripe" in resp.content


@pytest.mark.django_db
def test_switching_provider_is_refused_until_configured(op_client, settings):
    settings.STRIPE_SECRET_KEY = ""
    op_client.post(
        "/manage/settings/", {"app_name": "Akilent", "billing_provider": "stripe"}
    )
    assert SiteSettings.load().billing_provider == "flutterwave"


@pytest.mark.django_db
def test_switching_provider_is_audited_and_customers_told(op_client, settings):
    settings.STRIPE_SECRET_KEY = "sk_test"
    settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
    with patch("apps.billing.tasks.notify_provider_change.delay") as notify:
        op_client.post(
            "/manage/settings/", {"app_name": "Akilent", "billing_provider": "stripe"}
        )
    assert SiteSettings.load().billing_provider == "stripe"
    notify.assert_called_once_with("flutterwave", "stripe")
    assert AdminAction.objects.filter(action="settings.billing_provider").exists()


@pytest.mark.django_db
def test_pilot_bulk_grant(op_client, business):
    PilotEnrollment.objects.create(account=business, wave=1)
    resp = op_client.post(
        "/manage/pilot/grant-credit/", {"amount": "150", "days": "365"}
    )
    assert resp.status_code == 302
    assert credit.balance(business) == Decimal("150.00")
    op_client.post(
        "/manage/pilot/grant-credit/", {"amount": "150"}
    )  # skipped the 2nd time
    assert credit.balance(business) == Decimal("150.00")


@pytest.mark.django_db
def test_customer_plans_page_shows_credit(client, business):
    credit.grant(business, "150")
    client.force_login(business.owner)
    resp = client.get("/billing/plans/")
    assert resp.status_code == 200
    assert b"Account credit" in resp.content


@pytest.mark.django_db
def test_checkout_paid_fully_by_credit_skips_the_provider(client, business):
    credit.grant(business, "150")
    pro = Plan.objects.create(slug="pro", name="Pro", price_monthly=Decimal("40"))
    client.force_login(business.owner)
    resp = client.get("/billing/checkout/?plan=pro&period=monthly")
    assert resp.status_code == 302 and "flutterwave" not in resp["Location"]
    sub = Subscription.objects.get(account=business)
    assert (
        sub.plan == pro
        and sub.status == Subscription.ACTIVE
        and sub.provider == "credit"
    )


@pytest.mark.django_db
def test_checkout_sends_reduced_amount_to_the_provider(client, business):
    credit.grant(business, "5")
    client.force_login(business.owner)
    with patch("apps.billing.providers.flutterwave.get_fw_client") as fw:
        fw.return_value.initialize_payment.return_value = "https://checkout.flw/abc"
        resp = client.get("/billing/checkout/?plan=starter&period=monthly")
    assert resp["Location"] == "https://checkout.flw/abc"
    kwargs = fw.return_value.initialize_payment.call_args.kwargs
    assert kwargs["amount"] == Decimal("14.00") and kwargs["currency"] == "USD"
    assert kwargs["tx_ref"].startswith("pay_")
