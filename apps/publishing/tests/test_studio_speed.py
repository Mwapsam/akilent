"""Content Studio speed: each tab's query budget, entitlement and site settings read once per
page, and the Server-Timing header that shows where a slow page spends its time.

On production every query is a round trip to the database (about 10 ms), so the budget is in
queries: a template change that adds a few fails here before anyone feels it.
"""

import re

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.publishing.tests.test_studio_overlay import shop  # noqa: F401 - fixture
from apps.publishing.tests.test_studio_review_fixes import _seed

pytestmark = pytest.mark.django_db

RELEASED = frozenset(
    {
        "cs_media_library",
        "cs_sales_report",
        "cs_scheduling",
        "cs_bio_page",
        "cs_analytics",
        "cs_ai_plus",
    }
)

# Measured 2026-11-05 with 12 posts, plus a little room. Lower one when a tab gets faster.
BUDGET = {
    "/publishing/": 26,
    "/publishing/planning/": 21,
    "/publishing/calendar/": 18,
    "/publishing/calendar/?view=month": 21,
    "/publishing/library/": 16,
    "/publishing/media/": 19,
    "/publishing/sales/": 22,
    "/publishing/channels/": 18,
    "/publishing/ai/": 23,
    "/publishing/bio/": 17,
    "/publishing/analytics/": 21,
    "/publishing/reconciliation/": 14,
}


@pytest.mark.parametrize("url", list(BUDGET))
def test_each_tab_stays_within_its_query_budget(shop, settings, url):  # noqa: F811
    account, client = shop
    settings.RELEASED_FEATURES = RELEASED
    _seed(account, 12)
    client.get(url)

    with CaptureQueriesContext(connection) as q:
        assert client.get(url).status_code == 200

    assert len(q) <= BUDGET[url], [x["sql"][:100] for x in q.captured_queries]


def _count(q, table):
    return sum(f'FROM "{table}"' in x["sql"] for x in q.captured_queries)


def test_a_page_reads_entitlement_and_site_settings_once(shop, settings):  # noqa: F811
    _, client = shop
    settings.RELEASED_FEATURES = RELEASED

    with CaptureQueriesContext(connection) as q:
        client.get("/publishing/analytics/")  # asked ten times before the memo

    assert _count(q, "billing_subscription") == 1
    assert _count(q, "billing_accountfeatureoverride") == 1
    assert _count(q, "core_sitesettings") == 1


def test_a_change_during_the_page_is_seen_at_once():
    from apps.accounts.models import Account
    from apps.billing import api as billing_api
    from apps.billing.models import ModuleSubscription
    from apps.core.request_context import request_memo

    account = Account.objects.create(company_name="Memo")
    with request_memo():
        assert billing_api.usable(account, "orders")
        ModuleSubscription.objects.update_or_create(
            account=account, module="commerce", defaults={"enabled": False}
        )
        assert not billing_api.usable(account, "orders")  # the save forgot it
        billing_api.set_owner_switch(account, "orders", True)
        assert billing_api.usable(account, "orders")


def test_outside_a_page_nothing_is_remembered():
    from apps.core.models import SiteSettings

    first = SiteSettings.load()
    assert SiteSettings.load() is not first


def test_server_timing_says_where_the_time_went(shop, settings):  # noqa: F811
    from django.test import Client

    _, client = shop
    settings.RELEASED_FEATURES = RELEASED

    header = client.get("/publishing/library/")["Server-Timing"]
    anonymous = Client().get("/auth/login/")

    assert re.fullmatch(r'db;dur=[\d.]+;desc="\d+ q", app;dur=[\d.]+', header)
    assert "Server-Timing" not in anonymous  # only for people signed in
