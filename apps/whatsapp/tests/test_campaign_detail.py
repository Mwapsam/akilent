"""Campaigns plan, Phase E: a campaign's page leads with what it came to.

- a banner with the outcome ("2 of 2 failed" + the reason + its fix while it lasts, or "Sent to
  2, 100% delivered"), then the funnel from the list to replies in place of five equal tiles;
- the message as previewed, each blank showing where its value comes from;
- recipient tabs with counts, a filter per failure reason, and the live poll keeping the counts
  current until it stops;
- a skeleton in the page's shape while it loads.
"""

import datetime

import pytest

from apps.whatsapp import campaign_ui
from apps.whatsapp.campaigns import delivery_stats_for
from apps.whatsapp.models import MessageLog, WhatsAppCampaign
from apps.whatsapp.tests.test_campaign_list import _reply
from apps.whatsapp.tests.test_campaign_results import (  # noqa: F401
    _campaign,
    _wa_urls,
    shop,
)

pytestmark = pytest.mark.django_db


def _page(client, campaign, query=""):
    return client.get(f"/campaigns/{campaign.pk}/{query}").content.decode()


def _between(page, start, end):
    part = page[page.index(start) :]
    return part[: part.index(end)]


# --- The outcome first -----------------------------------------------------------------


@_wa_urls
def test_all_failed_leads_with_the_reason_and_its_fix(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "failed"])
    banner = _between(_page(client, campaign), "data-campaign-banner", "funnel-title")
    assert 'data-campaign-banner="danger"' in _page(client, campaign)
    assert "2 of 2 failed" in banner
    assert "billing problem" in banner
    assert "Fix the payment method in Meta" in banner


@_wa_urls
def test_partly_sent_says_how_many_of_each(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "read"])
    page = _page(client, campaign)
    assert 'data-campaign-banner="warning"' in page
    assert "Sent to 1 of 2, 1 failed" in page


@_wa_urls
def test_all_sent_says_how_many_were_delivered(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read", "delivered", "sent"])
    page = _page(client, campaign)
    assert 'data-campaign-banner="success"' in page
    assert "Sent to 3, 67% delivered" in page


@_wa_urls
def test_the_fix_goes_once_a_message_has_gone_through_since(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed"])
    _campaign(account, template, clist, ["delivered"])
    banner = _between(_page(client, campaign), "data-campaign-banner", "funnel-title")
    assert "1 of 1 failed" in banner and "data-campaign-fix" not in banner


@_wa_urls
def test_no_banner_while_it_is_still_sending(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    campaign.status = WhatsAppCampaign.Status.SENDING
    campaign.save(update_fields=["status"])
    assert "data-campaign-banner" not in _page(client, campaign)


@_wa_urls
def test_the_funnel_runs_from_the_list_to_replies(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "read", "delivered"])
    _reply(campaign, datetime.timedelta(hours=1))
    funnel = _between(_page(client, campaign), "data-campaign-funnel", "</dl>")
    steps = [
        ("On the list", 3),
        ("Sent", 2),
        ("Delivered", 2),
        ("Read", 1),
        ("Replied", 1),
    ]
    for label, n in steps:
        assert f'<dt>{label}</dt><dd class="tabular-nums">{n}</dd>' in funnel, label
    assert 'style="width: 100%"' in funnel  # the list, the widest step
    assert "stat-card" not in _between(
        _page(client, campaign), 'id="campaign-live"', "data-campaign-funnel"
    )


def test_the_funnel_is_shares_of_the_widest_step(shop):  # noqa: F811
    _, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read", "failed", "failed", "sent"])
    steps = campaign_ui.funnel(
        campaign, delivery_stats_for([campaign.pk])[campaign.pk], replied=1
    )
    assert [(s["value"], s["pct"]) for s in steps] == [
        (4, 100),
        (2, 50),
        (1, 25),
        (1, 25),
        (1, 25),
    ]


# --- The message -----------------------------------------------------------------------


def test_each_blank_shows_where_its_value_comes_from(shop):  # noqa: F811
    _, account, template, clist = shop
    template.content = "Hi {{1}}, visit {{2}} or call {{3}}"
    template.variables = ["name", "shop", "phone"]
    template.save()
    campaign = _campaign(account, template, clist, ["read"])
    campaign.variable_mapping = {"name": "contact.first_name", "shop": "Cairo Road"}
    campaign.variable_fallbacks = {"name": "there"}
    campaign.save()
    body = campaign_ui.message_with_sources(campaign)["body"]
    assert body == (
        'Hi {Customer\'s first name, or "there"}, visit Cairo Road or call [phone]'
    )


@_wa_urls
def test_the_page_shows_the_message(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    message = _between(_page(client, campaign), "data-campaign-message", "</section>")
    assert "Download app" in message and "Get our app" in message


# --- Recipients ------------------------------------------------------------------------


@_wa_urls
def test_the_tabs_show_their_counts(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "failed", "read"])
    page = _page(client, campaign)
    assert (
        'Everyone <span id="rc-everyone" class="text-ink-subtle tabular-nums">3<'
        in page
    )
    assert 'Sent <span id="rc-sent" class="text-ink-subtle tabular-nums">1<' in page
    assert 'Failed <span id="rc-failed" class="text-ink-subtle tabular-nums">2<' in page
    assert (
        'Not sent <span id="rc-not_sent" class="text-ink-subtle tabular-nums">0<'
        in page
    )


@_wa_urls
def test_each_failure_reason_can_be_shown_on_its_own(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(
        account, template, clist, ["failed", "failed", "bad_number", "read"]
    )
    page = _page(client, campaign)
    groups = _between(page, "data-failure-groups", "</section>")
    assert 'data-failure-filter="131042"' in groups and "Show these 2" in groups
    assert 'data-failure-filter="131026"' in groups and "Show who" in groups

    billing = _page(client, campaign, "?outcome=failed&reason=131042")
    rows = _between(billing, "<tbody>", "</tbody>")
    assert rows.count('data-label="Customer"') == 2
    assert "billing problem" in rows and "number may not" not in rows
    assert "data-failure-filter-on" in billing and "Show every failure" in billing

    one = _page(client, campaign, "?outcome=failed&reason=131026")
    assert _between(one, "<tbody>", "</tbody>").count('data-label="Customer"') == 1

    # A reason that isn't one of this campaign's: every failure, no filter line.
    other = _page(client, campaign, "?outcome=failed&reason=999")
    assert _between(other, "<tbody>", "</tbody>").count('data-label="Customer"') == 3
    assert "data-failure-filter-on" not in other


@_wa_urls
def test_a_failure_stored_without_a_code_has_its_own_filter(shop):  # noqa: F811
    from apps.whatsapp.models import OutboundMessage

    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["bad_number", "bad_number"])
    first = campaign.recipients.order_by("pk").first()
    OutboundMessage.objects.filter(pk=first.message_id).update(error_code="")
    first.error = "Something odd happened"
    first.save(update_fields=["error"])

    groups = _between(_page(client, campaign), "data-failure-groups", "</section>")
    assert 'data-failure-filter="none"' in groups
    rows = _between(
        _page(client, campaign, "?outcome=failed&reason=none"), "<tbody>", "</tbody>"
    )
    assert rows.count('data-label="Customer"') == 1 and "Something odd" in rows


@_wa_urls
def test_the_live_poll_keeps_the_counts_current_until_it_stops(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read", "sent"])
    campaign.status = WhatsAppCampaign.Status.SENDING
    campaign.save(update_fields=["status"])
    resp = client.get(f"/campaigns/{campaign.pk}/?part=live", HTTP_HX_REQUEST="true")
    live = resp.content.decode()
    assert 'hx-trigger="every 5s"' in live
    assert (
        '<span id="rc-sent" class="text-ink-subtle tabular-nums" hx-swap-oob="true">2<'
        in live
    )
    assert resp["Cache-Control"] == "no-store" and "HX-Request" in resp["Vary"]

    campaign.status = WhatsAppCampaign.Status.COMPLETED
    campaign.save(update_fields=["status"])
    live = client.get(
        f"/campaigns/{campaign.pk}/?part=live", HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "hx-trigger" not in live and "data-campaign-banner" in live
    # The full page's own counts are never out of band.
    assert "hx-swap-oob" not in _page(client, campaign)


# --- Loading ---------------------------------------------------------------------------


@_wa_urls
def test_opening_a_campaign_shows_its_shape_while_it_loads(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    listing = client.get("/campaigns/?channel=whatsapp").content.decode()
    link = f'href="/campaigns/{campaign.pk}/" data-skeleton="campaign"'
    assert link in listing
    assert '<template id="skeleton-campaign">' in listing


@_wa_urls
def test_a_reply_before_the_message_doesnt_count_in_the_funnel(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    _reply(campaign, -datetime.timedelta(minutes=5))
    assert (
        MessageLog.objects.filter(direction=MessageLog.Direction.INBOUND).count() == 1
    )
    funnel = _between(_page(client, campaign), "data-campaign-funnel", "</dl>")
    assert '<dt>Replied</dt><dd class="tabular-nums">0</dd>' in funnel
