from decimal import Decimal

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
    return create_order(
        account,
        contact,
        items=[
            {"name": "Chicken Burger", "unit_price": Decimal("75.00"), "quantity": 2},
        ],
    )


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
    other_order = create_order(
        other,
        other_contact,
        items=[
            {"name": "X", "unit_price": Decimal("1.00"), "quantity": 1},
        ],
    )
    resp = client.get(f"/orders/{other_order.public_id}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_create_order_via_orders_page(logged_in, contact):
    client, account, _ = logged_in
    resp = client.post(
        "/orders/create/",
        {
            "contact": contact.phone,
            "name": "Website package",
            "unit_price": "500",
            "quantity": "1",
            "currency": "USD",
        },
    )
    assert resp.status_code == 302
    order = Order.objects.get(account=account, contact=contact)
    assert order.total == Decimal("500.00")
    assert resp["Location"] == f"/orders/{order.public_id}/"


@pytest.mark.django_db
def test_create_order_unknown_contact_shows_error(logged_in):
    client, account, _ = logged_in
    resp = client.post(
        "/orders/create/",
        {"contact": "+000000000", "name": "X", "unit_price": "1", "quantity": "1"},
        follow=True,
    )
    assert resp.status_code == 200
    assert b"No customer found" in resp.content
    assert not Order.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_create_order_invalid_price_shows_error(logged_in, contact):
    client, account, _ = logged_in
    resp = client.post(
        "/orders/create/",
        {
            "contact": contact.phone,
            "name": "X",
            "unit_price": "not-a-number",
            "quantity": "1",
        },
        follow=True,
    )
    assert resp.status_code == 200
    assert b"valid price" in resp.content
    assert not Order.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_mark_paid_and_unpaid_via_view(logged_in, order):
    client, _, _ = logged_in
    page = client.get(f"/orders/{order.public_id}/")
    assert b"Mark as paid" in page.content
    assert b"Request payment" not in page.content

    resp = client.post(
        f"/orders/{order.public_id}/",
        {"action": "mark_paid", "method": "airtel_money", "reference": "AM-12"},
    )
    assert resp.status_code == 302
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert (order.paid_method, order.paid_reference) == ("airtel_money", "AM-12")
    page = client.get(f"/orders/{order.public_id}/")
    assert b"Airtel Money" in page.content and b"AM-12" in page.content

    client.post(f"/orders/{order.public_id}/", {"action": "mark_unpaid"})
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING


@pytest.mark.django_db
def test_an_order_paid_online_through_an_old_link_cannot_be_marked_unpaid(
    logged_in, order
):
    from django.utils import timezone

    from apps.commerce.models import Payment

    client, account, _ = logged_in
    Payment.objects.create(
        account=account,
        order=order,
        amount=order.total,
        status=Payment.Status.SUCCEEDED,
        transaction_id="fw-1",
    )
    Order.objects.filter(pk=order.pk).update(
        status=Order.Status.PAID, paid_at=timezone.now()
    )
    page = client.get(f"/orders/{order.public_id}/")
    assert b"Paid online" in page.content
    assert b"Mark as not paid" not in page.content

    client.post(f"/orders/{order.public_id}/", {"action": "mark_unpaid"})
    order.refresh_from_db()
    assert order.status == Order.Status.PAID


@pytest.mark.django_db
def test_request_payment_post_does_nothing_now(logged_in, order):
    client, _, _ = logged_in
    resp = client.post(f"/orders/{order.public_id}/", {"action": "request_payment"})
    assert resp.status_code == 302
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING
    assert not order.payments.exists()
