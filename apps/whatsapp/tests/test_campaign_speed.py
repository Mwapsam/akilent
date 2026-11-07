"""Campaigns plan: query budgets for the list, a campaign's page and the composer.

Measured 2026-11-06 (Phase A) with a billing failure on record, so the account-problem check
is counted. Lower a number when a page gets faster; never raise one to make a change pass.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.whatsapp.tests.test_campaign_results import (  # noqa: F401
    _campaign,
    _wa_urls,
    shop,
)

pytestmark = pytest.mark.django_db

BUDGET = {
    "list": 16,
    "detail": 19,
    "new": 17,
    "settings": 13,
}
# list 15 -> 12: one query for the tab counts (Phase C). 12 -> 16 (Phase D, new content, not
# a slowdown): replies, the last campaign's failure reasons, and for the tiles the workspace's
# timezone and one query for 8 weeks of messages (15 with the start card in their place).
# detail 19 -> 20 (Phase E): who replied, for the funnel.
# Phase G: list 16 -> 15 (the email plan check only on the Email tab); detail 20 -> 19 (one
# count by status and skip reason, one "still waiting" check for Pause and Cancel, with the
# workspace's timezone added for local times); new 19 -> 17 (readiness from the templates
# already loaded); settings measured at 13.
# list 15 -> 16 (Phase H, new content): orders credited to a campaign, for the Orders tile and
# column (the column reuses the tile's for recent campaigns, as Replied does).


def _urls(campaign):
    return {
        "list": "/campaigns/?channel=whatsapp",
        "detail": f"/campaigns/{campaign.pk}/",
        "new": "/campaigns/new/",
        "settings": "/campaigns/settings/",
    }


def _count(client, url):
    client.get(url)  # warm per-process caches, as a real second visit would
    with CaptureQueriesContext(connection) as ctx:
        assert client.get(url).status_code == 200
    return len(ctx.captured_queries)


@_wa_urls
@pytest.mark.parametrize("name", list(BUDGET))
def test_each_campaign_page_stays_within_its_budget(shop, name):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "read", "delivered"])
    assert _count(client, _urls(campaign)[name]) <= BUDGET[name]


@_wa_urls
@pytest.mark.parametrize("name", list(BUDGET))
def test_the_counts_dont_grow_with_campaigns_or_recipients(shop, name):  # noqa: F811
    client, account, template, clist = shop
    for _ in range(3):  # enough for the list's tiles, so both counts take the same path
        small = _campaign(account, template, clist, ["failed", "read"])
    before = _count(client, _urls(small)[name])
    for _ in range(5):
        big = _campaign(account, template, clist, ["failed", "bad_number", "read"] * 4)
    assert _count(client, _urls(big)[name]) == before


@_wa_urls
def test_the_start_card_doesnt_grow_with_recipients(shop):  # noqa: F811
    """Before the tiles: the last campaign and the starting points, whatever their size."""
    client, account, template, clist = shop
    small = _campaign(account, template, clist, ["failed", "read"])
    before = _count(client, _urls(small)["list"])
    small.delete()
    big = _campaign(account, template, clist, ["failed", "bad_number", "read"] * 20)
    assert _count(client, _urls(big)["list"]) == before
