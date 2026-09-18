import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.automation.models import Workflow


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.mark.django_db
def test_templates_page_lists_verticals(logged_in):
    client, _, _ = logged_in
    resp = client.get("/automations/templates/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Restaurant" in body and "Real Estate" in body


@pytest.mark.django_db
def test_activate_via_view_creates_workflows_and_redirects(logged_in):
    client, account, _ = logged_in
    resp = client.post("/automations/templates/restaurant/activate/")
    assert resp.status_code == 302
    assert Workflow.objects.filter(account=account, slug__startswith="restaurant-").count() == 2

    resp = client.get("/automations/templates/")
    assert "Activated" in resp.content.decode()


@pytest.mark.django_db
def test_activate_unknown_vertical_via_view_shows_error(logged_in):
    client, _, _ = logged_in
    resp = client.post("/automations/templates/not-real/activate/")
    assert resp.status_code == 302
