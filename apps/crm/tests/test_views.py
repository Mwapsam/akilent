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
    deal = convert_lead_to_deal(lead, title="First deal")

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
    resp = client.post("/sales/leads/create/", {"contact": contact.phone, "source": "Website package"})
    assert resp.status_code == 302
    lead = Lead.objects.get(account=account, contact=contact)
    assert lead.source == "Website package"
    assert "/sales/leads/" in resp["Location"]


@pytest.mark.django_db
def test_create_lead_with_value_creates_a_deal_directly(logged_in, contact):
    client, account, _ = logged_in
    resp = client.post("/sales/leads/create/", {"contact": contact.phone, "value": "500"})
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

    resp = client.post("/sales/leads/create/", {"contact": other_contact.phone}, follow=True)
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
