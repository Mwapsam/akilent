from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.commerce.models import Order
from apps.commerce.services import create_order
from apps.contacts.models import Contact


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.fixture
def contact(logged_in):
    _, account, _ = logged_in
    return Contact.objects.create(account=account, phone="+260971234567")


@pytest.fixture
def order(logged_in, contact):
    _, account, _ = logged_in
    return create_order(account, contact, items=[
        {"name": "Chicken Burger", "unit_price": Decimal("75.00"), "quantity": 2},
    ])


@pytest.mark.django_db
def test_orders_page_lists_needs_attention(logged_in, order):
    client, _, _ = logged_in
    resp = client.get("/orders/")
    assert resp.status_code == 200
    assert order.public_id in resp.content.decode()


@pytest.mark.django_db
def test_order_detail_scoped_to_account(logged_in):
    client, _, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260970000000")
    other_order = create_order(other, other_contact, items=[
        {"name": "X", "unit_price": Decimal("1.00"), "quantity": 1},
    ])
    resp = client.get(f"/orders/{other_order.public_id}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_request_payment_via_view(logged_in, order):
    client, _, _ = logged_in
    with patch("apps.billing.flutterwave.get_fw_client") as fw:
        fw.return_value.initialize_payment.return_value = "https://pay.example/xyz"
        resp = client.post(f"/orders/{order.public_id}/", {"action": "request_payment"})

    assert resp.status_code == 302
    order.refresh_from_db()
    assert order.status == Order.Status.AWAITING_PAYMENT
