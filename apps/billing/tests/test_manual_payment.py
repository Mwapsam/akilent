from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.core import mail

from apps.accounts.models import Account, Membership
from apps.billing.models import ManualPaymentRequest, PaymentMethod, Plan

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
def admin_user(db):
    return User.objects.create_user(
        username="admin1",
        password="pw12345!",
        email="admin@example.com",
        is_superuser=True,
    )


@pytest.mark.django_db
def test_manual_submit_creates_pending_request_and_notifies_admin(
    client, account, owner, plan, manual_method, admin_user
):
    client.force_login(owner)
    resp = client.post(MANUAL_SUBMIT_URL, {"plan": plan.slug, "reference": "TXN-123"})

    assert resp.status_code == 302
    req = ManualPaymentRequest.objects.get(account=account, plan=plan)
    assert req.status == ManualPaymentRequest.PENDING
    assert req.reference == "TXN-123"

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["admin@example.com"]
    assert "Manual Pay Co" in mail.outbox[0].body


@pytest.mark.django_db
def test_manual_submit_requires_reference(
    client, account, owner, plan, manual_method, admin_user
):
    client.force_login(owner)
    resp = client.post(MANUAL_SUBMIT_URL, {"plan": plan.slug, "reference": ""})

    assert resp.status_code == 302
    assert not ManualPaymentRequest.objects.filter(account=account).exists()
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_manual_submit_disabled_method_rejected(
    client, account, owner, plan, admin_user
):
    # No enabled PaymentMethod(code="manual") fixture here.
    client.force_login(owner)
    resp = client.post(MANUAL_SUBMIT_URL, {"plan": plan.slug, "reference": "TXN-123"})

    assert resp.status_code == 302
    assert not ManualPaymentRequest.objects.filter(account=account).exists()
    assert len(mail.outbox) == 0
