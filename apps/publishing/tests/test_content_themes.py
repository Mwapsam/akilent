"""Audit 2026-10-19, phase 12: publishing "campaigns" are content themes, with
edit, delete and dates — and deleting one keeps its posts."""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentCampaign


@pytest.fixture
def owner(client, db):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


@pytest.mark.django_db
def test_add_edit_and_delete_a_theme(owner):
    client, account = owner
    url = reverse("publishing:campaigns")
    assert "Content themes" in client.get(url).content.decode()

    client.post(url, {"action": "create", "name": "Launch", "start_date": "2026-11-01"})
    theme = ContentCampaign.objects.get(account=account)
    assert theme.start_date.isoformat() == "2026-11-01"

    client.post(
        url,
        {
            "action": "update",
            "pk": theme.pk,
            "name": "Big launch",
            "start_date": "2026-11-05",
            "end_date": "2026-11-01",
        },
    )
    theme.refresh_from_db()
    assert theme.name == "Launch"  # end before start: refused

    client.post(url, {"action": "update", "pk": theme.pk, "name": "Big launch"})
    theme.refresh_from_db()
    assert theme.name == "Big launch" and theme.start_date is None

    other = ContentCampaign.objects.create(
        account=Account.objects.create(company_name="X"), name="Theirs"
    )
    client.post(url, {"action": "delete", "pk": other.pk})
    assert ContentCampaign.objects.filter(pk=other.pk).exists()
    client.post(url, {"action": "delete", "pk": theme.pk})
    assert not ContentCampaign.objects.filter(pk=theme.pk).exists()
