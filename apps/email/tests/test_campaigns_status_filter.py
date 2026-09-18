import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.email.models import BulkEmailCampaign


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _campaign(account, status, from_email="a@acme.test"):
    return BulkEmailCampaign.objects.create(
        account=account, from_email=from_email, status=status
    )


@pytest.mark.django_db
def test_campaigns_list_defaults_to_all(logged_in):
    client, account = logged_in
    _campaign(account, BulkEmailCampaign.Status.DRAFT)
    _campaign(account, BulkEmailCampaign.Status.SCHEDULED)
    r = client.get("/email/campaigns/")
    assert r.status_code == 200
    assert len(r.context["campaigns"]) == 2


@pytest.mark.django_db
def test_campaigns_list_filters_by_scheduled_status(logged_in):
    client, account = logged_in
    _campaign(account, BulkEmailCampaign.Status.DRAFT)
    scheduled = _campaign(account, BulkEmailCampaign.Status.SCHEDULED)
    r = client.get("/email/campaigns/?status=scheduled")
    assert r.status_code == 200
    campaigns = list(r.context["campaigns"])
    assert campaigns == [scheduled]


@pytest.mark.django_db
def test_campaigns_list_sending_combines_queued_and_sending(logged_in):
    client, account = logged_in
    queued = _campaign(account, BulkEmailCampaign.Status.QUEUED)
    sending = _campaign(account, BulkEmailCampaign.Status.SENDING)
    _campaign(account, BulkEmailCampaign.Status.COMPLETED)
    r = client.get("/email/campaigns/?status=sending")
    assert r.status_code == 200
    assert set(r.context["campaigns"]) == {queued, sending}


@pytest.mark.django_db
def test_campaigns_list_failed_still_reachable_via_all(logged_in):
    client, account = logged_in
    failed = _campaign(account, BulkEmailCampaign.Status.FAILED)
    r = client.get("/email/campaigns/")
    assert failed in list(r.context["campaigns"])
