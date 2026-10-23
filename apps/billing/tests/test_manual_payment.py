from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.core import mail

from apps.accounts.models import Account, Membership
from apps.billing import payments
from apps.billing.models import (
    BillingPayment,
    ManualPaymentRequest,
    PaymentMethod,
    Plan,
    Subscription,
)

MANUAL_SUBMIT_URL = "/billing/manual/submit/"


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Manual Pay Co")


@pytest.fixture
def owner(db, account):
    user = User.objects.create_user(username="owner1", password="pw12345!")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user


@pytest.fixture
def plan(db):
    return Plan.objects.create(
        slug="starter", name="Starter", price_monthly=Decimal("19")
    )


@pytest.fixture
def manual_method(db):
    # The 0010_seed_payment_methods migration already creates this row
    # (disabled by default) — update it in place rather than re-creating.
    method, _ = PaymentMethod.objects.update_or_create(
        code="manual",
        defaults={
            "name": "Bank transfer",
            "is_enabled": True,
            "instructions": "Wire to Acme Bank.",
        },
    )
    return method


@pytest.fixture
def pending(db, account, plan):
    return payments.create_payment(account, plan, "quarterly", provider="manual")


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="admin1",
        password="pw12345!",
        email="admin@example.com",
        is_superuser=True,
    )


@pytest.mark.django_db
def test_manual_submit_creates_pending_request_and_notifies_admin(
    client, account, owner, plan, manual_method, admin_user, pending
):
    client.force_login(owner)
    resp = client.post(
        MANUAL_SUBMIT_URL, {"payment": pending.reference, "reference": "TXN-123"}
    )

    assert resp.status_code == 302
    req = ManualPaymentRequest.objects.get(account=account, plan=plan)
    assert req.status == ManualPaymentRequest.PENDING
    assert req.reference == "TXN-123"
    assert req.payment == pending and req.billing_period == "quarterly"
    assert "/manage/payments/" in mail.outbox[0].body

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["admin@example.com"]
    assert "Manual Pay Co" in mail.outbox[0].body


@pytest.mark.django_db
def test_manual_submit_requires_reference(
    client, account, owner, plan, manual_method, admin_user, pending
):
    client.force_login(owner)
    resp = client.post(
        MANUAL_SUBMIT_URL, {"payment": pending.reference, "reference": ""}
    )

    assert resp.status_code == 302
    assert not ManualPaymentRequest.objects.filter(account=account).exists()
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_manual_submit_disabled_method_rejected(
    client, account, owner, plan, admin_user, pending
):
    # No enabled PaymentMethod(code="manual") fixture here.
    client.force_login(owner)
    resp = client.post(
        MANUAL_SUBMIT_URL, {"payment": pending.reference, "reference": "TXN-123"}
    )

    assert resp.status_code == 302
    assert not ManualPaymentRequest.objects.filter(account=account).exists()
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_approval_grants_the_period_paid_for(
    client, account, owner, plan, manual_method, admin_user, pending
):
    Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.INCOMPLETE,
        current_period_start=pending.created_at,
    )
    client.force_login(owner)
    client.post(MANUAL_SUBMIT_URL, {"payment": pending.reference, "reference": "TXN-9"})
    req = ManualPaymentRequest.objects.get(account=account)

    client.force_login(admin_user)
    client.post(f"/manage/payments/{req.pk}/approve/")

    sub = Subscription.objects.get(account=account)
    pending.refresh_from_db()
    assert sub.status == Subscription.ACTIVE
    assert sub.billing_period == "quarterly" and sub.provider == "manual"
    assert pending.status == BillingPayment.SUCCEEDED


@pytest.mark.django_db
def test_cannot_submit_against_another_business_payment(
    client, owner, plan, manual_method, admin_user
):
    other = Account.objects.create(company_name="Other Co")
    theirs = payments.create_payment(other, plan, "monthly", provider="manual")
    client.force_login(owner)
    resp = client.post(
        MANUAL_SUBMIT_URL, {"payment": theirs.reference, "reference": "X"}
    )
    assert resp.status_code == 404
    assert not ManualPaymentRequest.objects.exists()
