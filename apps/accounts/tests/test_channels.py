import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


@pytest.mark.django_db
def test_channels_page_loads(logged_in):
    client, account = logged_in
    r = client.get("/channels/")
    assert r.status_code == 200
    assert b"Channels" in r.content
    assert b"Email" in r.content


@pytest.mark.django_db
def test_channels_hides_whatsapp_card_when_disabled(settings, logged_in):
    settings.WHATSAPP_ENABLED = False
    client, account = logged_in
    r = client.get("/channels/")
    assert r.status_code == 200
    assert b"Manage WhatsApp" not in r.content
