from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Account, Membership
from apps.publishing.models import FacebookPageConnection
from apps.publishing.providers.facebook import FacebookPublishError

pytestmark = pytest.mark.django_db


@pytest.fixture
def pending(settings):
    settings.WHATSAPP_APP_ID = "app"
    settings.WHATSAPP_APP_SECRET = "secret"
    user = User.objects.create_user("owner", "owner@example.com", "pass")
    account = Account.objects.create(company_name="Page Co")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    conn = FacebookPageConnection.objects.create(account=account, oauth_state="s1")
    client = Client()
    client.force_login(user)
    return client, conn


def _code_exchange(token="short-user"):
    return MagicMock(json=lambda: {"access_token": token})


def test_page_token_is_fetched_with_a_long_lived_user_token(pending):
    client, conn = pending
    pages = [{"id": "p1", "name": "Page", "access_token": "page-token"}]
    with (
        patch("apps.publishing.views.requests.get", return_value=_code_exchange()),
        patch(
            "apps.publishing.views.exchange_for_long_lived_user_token",
            return_value="long-user",
        ) as exchange,
        patch("apps.publishing.views.list_pages", return_value=pages) as list_pages,
    ):
        client.get(reverse("publishing:fb-callback"), {"code": "c", "state": "s1"})

    exchange.assert_called_once_with("app", "secret", "short-user")
    list_pages.assert_called_once_with("long-user")
    conn.refresh_from_db()
    assert conn.status == FacebookPageConnection.Status.ACTIVE
    assert conn.access_token == "page-token"
    assert conn.token_expires_at is None


def test_failed_long_lived_exchange_does_not_store_a_short_lived_token(pending):
    client, conn = pending
    with (
        patch("apps.publishing.views.requests.get", return_value=_code_exchange()),
        patch(
            "apps.publishing.views.exchange_for_long_lived_user_token",
            side_effect=FacebookPublishError("bad token"),
        ),
        patch("apps.publishing.views.list_pages") as list_pages,
    ):
        client.get(reverse("publishing:fb-callback"), {"code": "c", "state": "s1"})

    list_pages.assert_not_called()
    conn.refresh_from_db()
    assert not conn.access_token
    assert not conn.page_id


def test_empty_state_is_rejected_after_a_completed_connect(pending):
    client, conn = pending
    conn.oauth_state = ""
    conn.save(update_fields=["oauth_state"])

    with patch("apps.publishing.views.requests.get") as get:
        client.get(reverse("publishing:fb-callback"), {"code": "c", "state": ""})

    get.assert_not_called()
