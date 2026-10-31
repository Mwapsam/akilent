import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.crm.models import Deal, Lead
from apps.crm.services import convert_lead_to_deal, create_lead


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Acme Real Estate")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.fixture
def contact(logged_in):
    _, account, _ = logged_in
    return Contact.objects.create(account=account, phone="+260971111111")


@pytest.mark.django_db
def test_sales_page_lists_open_leads(logged_in, contact):
    client, account, _ = logged_in
    create_lead(account, contact, source="whatsapp")

    resp = client.get("/sales/")
    assert resp.status_code == 200
    assert "whatsapp" in resp.content.decode()


@pytest.mark.django_db
def test_sales_page_shows_pipeline_once_a_deal_exists(logged_in, contact):
    client, account, _ = logged_in
    lead = create_lead(account, contact)
    convert_lead_to_deal(lead, title="First deal")

    resp = client.get("/sales/")
    assert resp.status_code == 200
    assert "First deal" in resp.content.decode()


@pytest.mark.django_db
def test_lead_detail_scoped_to_account(logged_in):
    client, _, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260970000001")
    other_lead = create_lead(other, other_contact)

    resp = client.get(f"/sales/leads/{other_lead.public_id}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_convert_lead_via_view(logged_in, contact):
    client, account, _ = logged_in
    lead = create_lead(account, contact)

    resp = client.post(
        f"/sales/leads/{lead.public_id}/",
        {"action": "convert", "title": "New house", "value": "10000"},
    )
    assert resp.status_code == 302
    lead.refresh_from_db()
    assert lead.status == Lead.Status.CONVERTED
    assert Deal.objects.filter(account=account, title="New house").exists()


@pytest.mark.django_db
def test_create_lead_via_sales_page(logged_in, contact):
    client, account, _ = logged_in
    resp = client.post(
        "/sales/leads/create/", {"contact": contact.phone, "source": "Website package"}
    )
    assert resp.status_code == 302
    lead = Lead.objects.get(account=account, contact=contact)
    assert lead.source == "Website package"
    assert "/sales/leads/" in resp["Location"]


@pytest.mark.django_db
def test_create_lead_with_value_creates_a_deal_directly(logged_in, contact):
    client, account, _ = logged_in
    resp = client.post(
        "/sales/leads/create/", {"contact": contact.phone, "value": "500"}
    )
    assert resp.status_code == 302
    assert "/sales/deals/" in resp["Location"]
    assert Deal.objects.filter(account=account, contact=contact, value="500").exists()


@pytest.mark.django_db
def test_create_lead_unknown_contact_shows_error(logged_in):
    client, account, _ = logged_in
    resp = client.post("/sales/leads/create/", {"contact": "+000000000"}, follow=True)
    assert resp.status_code == 200
    assert b"No customer found" in resp.content
    assert not Lead.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_create_lead_cannot_use_another_accounts_contact(logged_in):
    client, account, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260970000002")

    resp = client.post(
        "/sales/leads/create/", {"contact": other_contact.phone}, follow=True
    )
    assert resp.status_code == 200
    assert b"No customer found" in resp.content
    assert not Lead.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_move_deal_stage_via_view(logged_in, contact):
    client, account, _ = logged_in
    lead = create_lead(account, contact)
    deal = convert_lead_to_deal(lead)
    won_stage = deal.pipeline.stages.get(is_won=True)

    resp = client.post(
        f"/sales/deals/{deal.public_id}/",
        {"action": "move_stage", "stage_id": won_stage.id},
    )
    assert resp.status_code == 302
    deal.refresh_from_db()
    assert deal.status == Deal.Status.WON


# --- Sales UI pass 2026-10-31: one track, interested → order ------------------------------


def _bought(account, contact):
    """A lead an order was created from (the lead page's "Create order")."""
    from decimal import Decimal

    from apps.commerce.services import create_order
    from apps.crm.services import mark_lead_bought

    lead = create_lead(account, contact)
    order = create_order(
        account,
        contact,
        items=[{"name": "Sandals", "unit_price": Decimal("40"), "quantity": 1}],
    )
    mark_lead_bought(lead, order)
    return lead, order


@pytest.mark.django_db
def test_sales_lists_bought_and_lost_under_their_own_tabs(logged_in, contact):
    client, account, _ = logged_in
    _bought(account, contact)
    lost_contact = Contact.objects.create(
        account=account, phone="+260972222222", first_name="Lulu"
    )
    lost = create_lead(account, lost_contact)
    Lead.objects.filter(pk=lost.pk).update(status=Lead.Status.LOST)

    open_body = client.get("/sales/").content.decode()
    assert "+260971111111" not in open_body  # bought: no longer still deciding
    bought = client.get("/sales/?view=bought").content.decode()
    assert "+260971111111" in bought and "Bought" in bought
    lost_body = client.get("/sales/?view=lost").content.decode()
    assert "Lulu" in lost_body


@pytest.mark.django_db
def test_sales_hides_the_stage_board_without_deals(logged_in, contact):
    client, account, _ = logged_in
    create_lead(account, contact)
    body = client.get("/sales/").content.decode()
    assert "Tracked by stage" not in body
    assert "Nothing to show yet" not in body


@pytest.mark.django_db
def test_lead_page_offers_the_order_as_the_next_step(logged_in, contact):
    from apps.conversations.models import Conversation

    client, account, _ = logged_in
    chat = Conversation.objects.create(account=account, contact=contact)
    lead = create_lead(account, contact, conversation_id=chat.public_id)

    body = client.get(f"/sales/leads/{lead.public_id}/").content.decode()
    assert "data-lead-create-order" in body
    assert f'name="lead" value="{lead.public_id}"' in body
    assert "data-lead-open-chat" in body and f"/inbox/{chat.public_id}/" in body
    # The deal title box no longer starts with the customer's phone or email in it.
    assert 'placeholder="+260971111111"' not in body
    assert 'placeholder="e.g. Leather sandals"' in body


@pytest.mark.django_db
def test_bought_lead_page_shows_its_order(logged_in, contact):
    client, account, _ = logged_in
    lead, order = _bought(account, contact)
    body = client.get(f"/sales/leads/{lead.public_id}/").content.decode()
    assert "They bought" in body and f"/orders/{order.public_id}/" in body
    assert "data-lead-create-order" not in body


@pytest.mark.django_db
def test_a_bought_lead_cannot_be_moved_back(logged_in, contact):
    from apps.crm.services import set_lead_status

    _, account, _ = logged_in
    lead, _ = _bought(account, contact)
    with pytest.raises(ValueError, match="already bought"):
        set_lead_status(lead, Lead.Status.NEW)
