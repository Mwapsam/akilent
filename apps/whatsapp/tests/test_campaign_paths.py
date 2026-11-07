"""Campaigns plan, Phase C: one address, honest wording, Lists in Campaigns' context.

- every campaign page lives under /campaigns/; the old /email/campaigns/ and
  /whatsapp/campaigns/… addresses redirect there (a GET permanently, a POST keeping its method);
- the WhatsApp pages still answer only while WhatsApp is on;
- the list doesn't promise email while email is off, and every tab has its count;
- Lists opened from Campaigns keeps Campaigns in the sidebar and goes back there.
"""

import pytest

from apps.whatsapp.tests.test_campaign_results import (  # noqa: F401
    _campaign,
    _wa_urls,
    shop,
)

pytestmark = pytest.mark.django_db


@_wa_urls
def test_the_pages_live_under_campaigns(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    for url in (
        "/campaigns/",
        "/campaigns/?channel=whatsapp",
        "/campaigns/new/",
        "/campaigns/settings/",
        "/campaigns/lists/",
        f"/campaigns/{campaign.pk}/",
    ):
        assert client.get(url).status_code == 200, url


@_wa_urls
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/email/campaigns/", "/campaigns/"),
        (
            "/email/campaigns/?channel=whatsapp&wa_status=finished",
            "/campaigns/?channel=whatsapp&wa_status=finished",
        ),
        ("/whatsapp/campaigns/new/?list=3", "/campaigns/new/?list=3"),
        ("/whatsapp/campaigns/settings/", "/campaigns/settings/"),
        ("/whatsapp/campaigns/7/", "/campaigns/7/"),
        ("/whatsapp/campaigns/7/export.csv", "/campaigns/7/export.csv"),
    ],
)
def test_old_addresses_redirect_for_good(shop, old, new):  # noqa: F811
    client, *_ = shop
    resp = client.get(old)
    assert resp.status_code == 301 and resp["Location"] == new


@_wa_urls
def test_a_form_posted_to_an_old_address_still_posts(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    resp = client.post(f"/whatsapp/campaigns/{campaign.pk}/duplicate/")
    assert resp.status_code == 308
    assert resp["Location"] == f"/campaigns/{campaign.pk}/duplicate/"


@_wa_urls
def test_the_whatsapp_pages_need_whatsapp_on(shop, settings):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    settings.WHATSAPP_ENABLED = False
    assert client.get("/campaigns/new/").status_code == 404
    assert client.get(f"/campaigns/{campaign.pk}/").status_code == 404


@_wa_urls
def test_the_list_says_only_the_channels_that_are_on(shop, settings):  # noqa: F811
    client, *_ = shop
    settings.EMAIL_ENABLED = False
    page = client.get("/campaigns/?channel=whatsapp").content.decode()
    assert "Reach a group of customers with a message on WhatsApp." in page
    assert "by email" not in page

    settings.EMAIL_ENABLED = True
    page = client.get("/campaigns/?channel=whatsapp").content.decode()
    assert "by email or WhatsApp" in page


@_wa_urls
def test_every_tab_has_its_count(shop):  # noqa: F811
    client, account, template, clist = shop
    _campaign(account, template, clist, ["read"])
    _campaign(account, template, clist, ["failed"])
    page = client.get("/campaigns/?channel=whatsapp").content.decode()
    finished = page[page.index("wa_status=finished") :]
    finished = finished[: finished.index("</a>")]
    assert 'tabular-nums">2</span>' in finished


@_wa_urls
def test_lists_from_campaigns_stays_in_campaigns(shop):  # noqa: F811
    client, account, template, clist = shop
    _campaign(account, template, clist, ["read"])
    page = client.get("/campaigns/?channel=whatsapp").content.decode()
    assert 'href="/campaigns/lists/"' in page

    lists = client.get("/campaigns/lists/").content.decode()
    assert "&larr; Campaigns" in lists and "&larr; Customers" not in lists
    nav = lists[
        lists.index('href="/campaigns/"') - 300 : lists.index('href="/campaigns/"')
        + 300
    ]
    assert "nav-link-active" in nav

    from_customers = client.get("/contacts/lists/").content.decode()
    assert "&larr; Customers" in from_customers
