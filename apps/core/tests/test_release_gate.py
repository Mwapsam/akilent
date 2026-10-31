"""Built-but-unreleased features stay out of reach (docs/marketing/messaging.md section 4).

settings_test releases them all so their own tests can reach their pages; these tests switch
them back off, as on a deployment that hasn't released them.
"""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership


@pytest.fixture
def unreleased(settings):
    settings.RELEASED_FEATURES = frozenset()


@pytest.fixture
def owner_client(client, db):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    account = Account.objects.create(
        company_name="Acme",
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
        email_verified=True,
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "/support/",
        "/support/portal/",
        "/chatbot/",
        "/api/chat/init/",
        "/hubspot/oauth/connect/",
        "/hubspot/webhook/",
    ],
)
def test_unreleased_pages_are_404(owner_client, unreleased, path):
    assert owner_client.get(path).status_code == 404


@pytest.mark.django_db
def test_released_feature_pages_pass_through(owner_client, settings):
    settings.RELEASED_FEATURES = frozenset({"support"})
    assert owner_client.get("/support/").status_code != 404


@pytest.mark.django_db
def test_entry_points_are_hidden(owner_client, unreleased):
    dashboard = owner_client.get("/dashboard/").content.decode()
    assert 'href="/support/"' not in dashboard

    panels = owner_client.get("/dashboard/panels/").content.decode()
    assert "Create chatbot" not in panels
    assert "Chat widgets" not in panels

    integrations = owner_client.get("/settings/integrations/").content.decode()
    assert "Connect HubSpot" not in integrations


@pytest.mark.django_db
def test_entry_points_show_once_released(owner_client, settings):
    settings.RELEASED_FEATURES = frozenset({"support", "chatbot", "hubspot"})
    assert 'href="/support/"' in owner_client.get("/dashboard/").content.decode()
    assert (
        "Connect HubSpot"
        in owner_client.get("/settings/integrations/").content.decode()
    )
