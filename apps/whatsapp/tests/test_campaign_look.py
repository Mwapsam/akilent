"""Campaigns plan, Phase G: look, local times and the queries behind the list.

- every Campaigns page has Content Studio's header: its name in large type under a
  "Campaigns" crumb (none on the list itself), and Sending hours uses the shared switch;
- times read in the workspace's own zone, not the server's UTC;
- the list reuses the tiles' reply counts for campaigns inside their 8 weeks, and counts the
  older ones on their own, so both read the same;
- the composer's readiness comes from the templates a campaign can actually send.
"""

import datetime
from zoneinfo import ZoneInfo

import pytest

from apps.accounts.models import BusinessHours
from apps.whatsapp.models import MessageTemplate, WhatsAppCampaign
from apps.whatsapp.tests.test_campaign_composer import composer  # noqa: F401
from apps.whatsapp.tests.test_campaign_list import _reply
from apps.whatsapp.tests.test_campaign_results import (  # noqa: F401
    _campaign,
    _wa_urls,
    shop,
)

pytestmark = pytest.mark.django_db
LIST = "/campaigns/?channel=whatsapp"


def _head(page):
    start = page.index('<header class="cs-head')
    return page[start : page.index("</header>", start)]


@_wa_urls
def test_each_page_has_the_shared_header(composer):  # noqa: F811
    client, account, template, clist = composer
    campaign = _campaign(account, template, clist, ["read"])
    pages = {
        LIST: ("Campaigns", False),
        f"/campaigns/{campaign.pk}/": ("App Download", True),
        "/campaigns/new/": ("New WhatsApp campaign", True),
        "/campaigns/settings/": ("Sending hours", True),
    }
    for url, (title, crumb) in pages.items():
        head = _head(client.get(url).content.decode())
        assert f">{title}</h1>" in head and 'class="cs-h1' in head, url
        assert ('class="cs-crumb">Campaigns</a>' in head) is crumb, url


@_wa_urls
def test_sending_hours_uses_the_shared_switch(shop):  # noqa: F811
    client, *_ = shop
    page = client.get("/campaigns/settings/").content.decode()
    assert 'type="checkbox" role="switch" name="enabled" class="switch"' in page


@_wa_urls
def test_times_read_in_the_workspaces_zone(shop):  # noqa: F811
    client, account, template, clist = shop
    BusinessHours.objects.create(account=account, timezone="Africa/Lusaka", schedule={})
    campaign = _campaign(account, template, clist, ["read"])
    late = datetime.datetime(
        2026, 11, 6, 22, 30, tzinfo=datetime.UTC
    )  # 00:30 in Lusaka
    WhatsAppCampaign.objects.filter(pk=campaign.pk).update(started_at=late)

    detail = client.get(f"/campaigns/{campaign.pk}/").content.decode()
    assert "sent 7 Nov 2026, 00:30" in detail and "6 Nov 2026, 22:30" not in detail
    listing = client.get(LIST).content.decode()
    assert "7 Nov, 00:30" in listing and "6 Nov, 22:30" not in listing


@_wa_urls
def test_replies_read_the_same_inside_and_outside_the_tiles_weeks(shop):  # noqa: F811
    client, account, template, clist = shop
    recent = [_campaign(account, template, clist, ["read", "read"]) for _ in range(3)]
    old = _campaign(account, template, clist, ["read", "read"])
    long_ago = datetime.datetime.now(tz=ZoneInfo("UTC")) - datetime.timedelta(days=120)
    WhatsAppCampaign.objects.filter(pk=old.pk).update(started_at=long_ago)
    from apps.whatsapp.models import MessageLog, OutboundMessage

    OutboundMessage.objects.filter(campaign_recipient__campaign=old).update(
        sent_at=long_ago
    )
    MessageLog.objects.filter(outbound_source__campaign_recipient__campaign=old).update(
        timestamp=long_ago
    )
    _reply(recent[0], datetime.timedelta(hours=1))
    _reply(old, datetime.timedelta(hours=1))

    page = client.get(LIST).content.decode()
    assert "data-campaign-tiles" in page

    def replied(campaign):
        # The row's own link (the "Last campaign" tile links there too).
        row = page[
            page.index(
                f'href="/campaigns/{campaign.pk}/" data-skeleton="campaign" class="font-medium'
            ) :
        ]
        row = row[: row.index("</tr>")]
        return (
            row[row.index('data-label="Replied"') :].split(">", 1)[1].split("<", 1)[0]
        )

    assert replied(recent[0]) == "50%"  # from the tiles' query
    assert replied(old) == "50%"  # counted on its own
    assert replied(recent[1]) == "0%"


@_wa_urls
def test_a_one_time_code_template_alone_is_not_ready(shop):  # noqa: F811
    from apps.whatsapp.models import WhatsAppBusinessNumber

    client, account, template, clist = shop
    WhatsAppBusinessNumber.objects.create(
        account=account, phone_number_id="PN1", waba_id="WABA1", is_active=True
    )
    template.category = MessageTemplate.Category.AUTHENTICATION
    template.save(update_fields=["category"])
    page = client.get("/campaigns/new/").content.decode()
    assert 'id="readiness-heading"' in page and 'id="composer-form"' not in page
