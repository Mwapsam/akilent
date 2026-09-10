"""Dynamic-data fetch: SSRF guard, caching, render injection."""
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, EmailTemplate, EmailTemplateDataSource
from apps.email.services.datafetch import DataFetchError, _assert_public_https, fetch_json
from apps.email.services.render import render_template


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def test_ssrf_guard_blocks_non_https_and_private_hosts():
    with pytest.raises(DataFetchError):
        _assert_public_https("http://example.com/data")
    with patch("apps.email.services.datafetch.socket.getaddrinfo",
               return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
        with pytest.raises(DataFetchError):
            _assert_public_https("https://internal.local/data")
    with patch("apps.email.services.datafetch.socket.getaddrinfo",
               return_value=[(2, 1, 6, "", ("10.0.0.5", 443))]):
        with pytest.raises(DataFetchError):
            _assert_public_https("https://metadata.example/data")


def test_fetch_json_caches_by_url():
    class _Resp:
        status_code = 200

        class raw:
            @staticmethod
            def read(n, decode_content=True):
                return b'{"total": 42}'

    with patch("apps.email.services.datafetch._assert_public_https"), \
         patch("apps.email.services.datafetch.requests.get", return_value=_Resp()) as get:
        a = fetch_json("https://api.example.com/order/1", ttl_seconds=60)
        b = fetch_json("https://api.example.com/order/1", ttl_seconds=60)
    assert a == {"total": 42} == b
    assert get.call_count == 1  # second call served from cache


@pytest.mark.django_db
def test_render_injects_data_source_and_survives_failure():
    acc = Account.objects.create(company_name="Acme")
    Plan.objects.create(slug="p", name="P", price_monthly=Decimal("1"))
    t = EmailTemplate.objects.create(account=acc, name="T", slug="t",
                                     subject="Order {{ order.id }}",
                                     html_body="<p>Total: {{ order.total }}</p>")
    EmailTemplateDataSource.objects.create(template=t, key="order",
                                           url="https://api.example.com/o/1")

    with patch("apps.email.services.datafetch.fetch_json", return_value={"id": "A1", "total": "€9"}):
        subj, _, html = render_template(t, {})
    assert subj == "Order A1"
    assert "Total: €9" in html

    # upstream failure -> {} injected, render still succeeds
    with patch("apps.email.services.datafetch.fetch_json", side_effect=DataFetchError("boom")):
        subj2, _, html2 = render_template(t, {})
    assert subj2 == "Order "


@pytest.fixture
def api_key(db):
    user = User.objects.create_user("o", "o@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"),
                               max_emails_per_month=100, email_apis=True, email_templates=True,
                               api_rate_per_min=0)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    k, raw = EmailApiKey.create_for_account(acc, name="k")
    k.scopes = ["messages:send", "templates:manage"]
    k.save(update_fields=["scopes"])
    return raw, acc


@pytest.mark.django_db
def test_data_source_api_rejects_private_url(client, api_key):
    key, acc = api_key
    EmailTemplate.objects.create(account=acc, name="T", slug="t", subject="Hi")
    r = client.put("/api/v1/templates/t/data-sources/order",
                   data={"url": "http://localhost/data"}, content_type="application/json",
                   HTTP_X_API_KEY=key)
    assert r.status_code == 400


@pytest.mark.django_db
def test_data_source_api_crud(client, api_key):
    key, acc = api_key
    EmailTemplate.objects.create(account=acc, name="T", slug="t", subject="Hi")
    with patch("apps.email.services.datafetch._assert_public_https"):
        put = client.put("/api/v1/templates/t/data-sources/order",
                         data={"url": "https://api.example.com/o", "ttl_seconds": 120},
                         content_type="application/json", HTTP_X_API_KEY=key)
    assert put.status_code == 200
    lst = client.get("/api/v1/templates/t/data-sources", HTTP_X_API_KEY=key)
    assert lst.json()["data"] == [{"key": "order", "url": "https://api.example.com/o", "ttl_seconds": 120}]
    assert client.delete("/api/v1/templates/t/data-sources/order", HTTP_X_API_KEY=key).status_code == 204
