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


# --- Sales UI pass 2026-10-31: lead → order, the list, the order page ----------------------


@pytest.mark.django_db
def test_create_order_from_a_lead_marks_it_bought(logged_in, contact):
    """The lead page's "Create order": the order is for the lead's customer and chat, the
    lead moves to Bought pointing at the order, and the next stop is the order page."""
    from apps.conversations.models import Conversation, Event
    from apps.crm.models import Lead, StageTransition
    from apps.crm.services import create_lead

    client, account, _ = logged_in
    chat = Conversation.objects.create(account=account, contact=contact)
    lead = create_lead(account, contact, conversation_id=chat.public_id)

    resp = client.post(
        "/orders/create/",
        {
            "lead": lead.public_id,
            "name": "Leather sandals",
            "unit_price": "40",
            "quantity": "2",
            "currency": "ZMW",
        },
    )

    order = Order.objects.get(account=account)
    assert resp.status_code == 302 and resp.url == f"/orders/{order.public_id}/"
    assert order.contact == contact and order.conversation == chat
    assert order.total == Decimal("80.00") and order.currency == "ZMW"
    lead.refresh_from_db()
    assert lead.status == Lead.Status.CONVERTED
    assert lead.attributes["order"] == order.public_id
    assert lead.converted_to_deal is None
    assert StageTransition.objects.filter(lead=lead, to_value="converted").exists()
    assert Event.objects.filter(
        type="lead.status_changed", subject_id=lead.public_id
    ).exists()

    page = client.get(f"/orders/{order.public_id}/").content.decode()
    assert "data-order-from-lead" in page


@pytest.mark.django_db
def test_create_order_refuses_another_accounts_lead(logged_in):
    from apps.crm.services import create_lead

    client, account, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    theirs = create_lead(other, Contact.objects.create(account=other, phone="+1555"))

    client.post(
        "/orders/create/",
        {"lead": theirs.public_id, "name": "X", "unit_price": "1", "quantity": "1"},
    )

    assert not Order.objects.exists()
    theirs.refresh_from_db()
    assert theirs.status == "new"


@pytest.mark.django_db
def test_orders_list_says_what_was_bought_and_links_the_chat(logged_in, contact):
    from apps.conversations.models import Conversation

    client, account, _ = logged_in
    chat = Conversation.objects.create(account=account, contact=contact)
    create_order(
        account,
        contact,
        items=[
            {"name": "Sandals", "unit_price": Decimal("40"), "quantity": 2},
            {"name": "Belt", "unit_price": Decimal("10"), "quantity": 1},
        ],
        conversation_id=chat.public_id,
    )
    body = client.get("/orders/").content.decode()
    assert "2 &times; Sandals" in body and "+1 more" in body
    assert f"/inbox/{chat.public_id}/" in body


@pytest.mark.django_db
def test_order_page_puts_payment_before_items(logged_in, order):
    client, _, _ = logged_in
    body = client.get(f"/orders/{order.public_id}/").content.decode()
    assert body.index(">Payment</h2>") < body.index(">Items</h2>")
    assert "Order for" in body


@pytest.mark.django_db
def test_new_orders_start_in_the_latest_orders_currency(logged_in, contact):
    client, account, _ = logged_in
    create_order(
        account,
        contact,
        items=[{"name": "A", "unit_price": Decimal("1"), "quantity": 1}],
        currency="ZMW",
    )
    body = client.get("/orders/?view=all").content.decode()
    assert 'name="currency" class="input" value="ZMW"' in body
