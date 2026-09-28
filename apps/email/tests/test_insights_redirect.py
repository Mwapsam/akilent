"""Insights moved to /insights/ (apps.conversations.insights_views); the old /email/insights/
URL redirects there and keeps ?domain=."""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


@pytest.mark.django_db
def test_old_insights_url_redirects_to_the_new_one(logged_in):
    client, account = logged_in
    resp = client.get("/email/insights/")
    assert resp.status_code == 302
    assert resp["Location"] == "/insights/"


@pytest.mark.django_db
def test_the_redirect_keeps_the_domain_query_param(logged_in):
    client, account = logged_in
    resp = client.get("/email/insights/?domain=example.com")
    assert resp.status_code == 302
    assert resp["Location"] == "/insights/?domain=example.com"
