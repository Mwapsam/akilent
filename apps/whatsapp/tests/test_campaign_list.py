"""Campaigns plan, Phase D: a list that shows what campaigns did.

- each row's Delivered, Read, Failed and Replied as shares of the messages it prepared;
- a reply is the customer writing back within 3 days of their campaign message (a fact from
  message times; crediting sales waits for a tracked code);
- tiles for this month with an 8-week trend once 3 campaigns have gone out;
- before that, how the last campaign went (with its fix) and three messages to start from.
"""

import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from apps.accounts.models import BusinessHours
from apps.core.charts import kpi_spark
from apps.whatsapp import campaign_ui
from apps.whatsapp.campaigns import delivery_stats_for, reply_counts_for
from apps.whatsapp.models import (
    MessageLog,
    MessageTemplate,
    OutboundMessage,
    WhatsAppCampaign,
    WhatsAppCampaignRecipient,
)
from apps.whatsapp.tests.test_campaign_composer import composer  # noqa: F401
from apps.whatsapp.tests.test_campaign_results import (  # noqa: F401
    _campaign,
    _wa_urls,
    shop,
)

pytestmark = pytest.mark.django_db
LIST = "/campaigns/?channel=whatsapp"


def _reply(campaign, after: datetime.timedelta, nth: int = 0):
    """The ``nth`` recipient who got the message writes back ``after`` it reached WhatsApp."""
    recipient = (
        campaign.recipients.filter(message__message_log__isnull=False)
        .select_related("message__message_log")
        .order_by("pk")[nth]
    )
    log = recipient.message.message_log
    return MessageLog.objects.create(
        account=campaign.account,
        conversation=log.conversation,
        contact=log.contact,
        direction=MessageLog.Direction.INBOUND,
        timestamp=log.timestamp + after,
        content="Yes please",
    )


# --- Shares ----------------------------------------------------------------------------


def test_shares_never_round_to_nothing_or_everything():
    assert campaign_ui.percent(0, 0) is None
    assert campaign_ui.percent(0, 5) == "0%"
    assert campaign_ui.percent(1, 300) == "<1%"
    assert campaign_ui.percent(299, 300) == "99%"
    assert campaign_ui.percent(2, 3) == "67%"
    assert campaign_ui.percent(4, 4) == "100%"


def test_rates_are_shares_of_the_messages_prepared(shop):  # noqa: F811
    _, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "read", "delivered"])
    r = campaign_ui.rates(delivery_stats_for([campaign.pk])[campaign.pk], replied=1)
    assert (r["prepared"], r["sent"], r["delivered"], r["read"]) == (3, 2, 2, 1)
    assert r["pct"] == {
        "delivered": "67%",
        "read": "33%",
        "failed": "33%",
        "replied": "33%",
    }


# --- Replies ---------------------------------------------------------------------------


def test_a_reply_is_writing_back_within_3_days(shop):  # noqa: F811
    _, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read", "delivered", "sent"])
    _reply(campaign, datetime.timedelta(hours=2), nth=0)  # counts
    _reply(campaign, datetime.timedelta(days=4), nth=1)  # too late
    _reply(campaign, -datetime.timedelta(hours=1), nth=2)  # before the message
    assert reply_counts_for([campaign.pk]) == {campaign.pk: 1}

    _reply(
        campaign, datetime.timedelta(days=2), nth=0
    )  # a second reply: still one person
    assert reply_counts_for([campaign.pk]) == {campaign.pk: 1}


def test_a_failed_message_has_no_reply(shop):  # noqa: F811
    _, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed"])
    assert reply_counts_for([campaign.pk]) == {campaign.pk: 0}


@_wa_urls
def test_the_list_shows_shares_and_how_replies_are_counted(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "read", "delivered"])
    _reply(campaign, datetime.timedelta(hours=1))
    page = client.get(LIST).content.decode()
    head = page[page.index("<thead>") : page.index("</thead>")]
    for column in ("Sent", "Delivered", "Read", "Failed", "Replied"):
        assert f">{column}" in head, column
    assert "within 3 days of their campaign message" in head
    assert "Not sent</th>" not in head

    row = page[page.index('data-label="Sent"') :]
    row = row[: row.index("</tr>")]
    assert ">2<" in row and "of 3" in row  # 2 accepted of 3 on the list
    assert 'data-label="Delivered" class="tabular-nums" title="2 of 3">67%' in row
    assert 'data-label="Replied" class="tabular-nums" title="1 of 3">33%' in row


# --- Tiles -----------------------------------------------------------------------------


@_wa_urls
def test_three_campaigns_get_tiles_with_a_trend(shop):  # noqa: F811
    client, account, template, clist = shop
    for _ in range(2):
        _campaign(account, template, clist, ["read", "delivered"])
    last = _campaign(account, template, clist, ["failed", "read"])
    last.name = "Weekend offer"
    last.save(update_fields=["name"])
    _reply(last, datetime.timedelta(hours=3))

    page = client.get(LIST).content.decode()
    assert "data-campaign-tiles" in page and "data-campaign-start" not in page
    tiles = page[page.index("data-campaign-tiles") :]
    tiles = tiles[: tiles.index("</section>")]

    def tile(key):
        t = tiles[tiles.index(f'data-kpi="{key}"') :]
        return t[: t.index('<div class="kpi"')] if '<div class="kpi"' in t else t

    assert 'kpi-value tabular-nums">5<' in tile("sent")
    assert "of 6 messages prepared" in tile("sent")
    assert 'kpi-value tabular-nums">83%<' in tile("delivered")
    assert 'kpi-value tabular-nums">1<' in tile("replied")
    assert "kpi-spark" in tile("sent") and "Sent in the week from" in tile("sent")
    latest = tile("latest")
    assert "Weekend offer" in latest and "Partly sent" in latest
    assert "Finished: 1 of 2 sent, 1 failed" in latest
    assert "Fix the payment method in Meta" in latest


@_wa_urls
def test_tiles_count_this_month_in_the_workspaces_own_days(shop):  # noqa: F811
    """00:30 on 1 November in Lusaka is this month there, though still 31 October in UTC."""
    _, account, template, clist = shop
    BusinessHours.objects.create(account=account, timezone="Africa/Lusaka", schedule={})
    lusaka = ZoneInfo("Africa/Lusaka")
    for _ in range(3):
        _campaign(account, template, clist, ["read"])
    first = datetime.datetime(2026, 11, 1, 0, 30, tzinfo=lusaka)
    MessageLog.objects.filter(direction=MessageLog.Direction.OUTBOUND).update(
        timestamp=first
    )
    OutboundMessage.objects.update(sent_at=first)
    with patch(
        "django.utils.timezone.now",
        return_value=datetime.datetime(2026, 11, 7, 10, 0, tzinfo=lusaka),
    ):
        tiles = {t["key"]: t for t in campaign_ui.summary(account)}
    assert tiles["sent"]["value"] == 3
    assert tiles["delivered"]["value"] == "100%"
    week = [s for s in tiles["sent"]["trend"]["slots"] if s["value"]]
    assert [(s["label"], s["value"]) for s in week] == [("26 Oct", 3)]


def test_a_week_with_nothing_sent_is_a_gap_not_a_zero():
    spark = kpi_spark([50, None, 100], ["a", "b", "c"], ["50%", "nothing sent", "100%"])
    assert spark["points"] == "0.0,14.0 100.0,2.0"
    assert spark["end"] == (100.0, 2.0)
    assert [s["value"] for s in spark["slots"]] == ["50%", "nothing sent", "100%"]
    assert kpi_spark([None, None], ["a", "b"])["end"] is None
    # One reading has no direction: no lone dot.
    assert kpi_spark([None, 80], ["a", "b"])["end"] is None


# --- Before there's a trend ------------------------------------------------------------


@_wa_urls
def test_a_first_campaign_shows_how_it_went_and_where_to_start(shop):  # noqa: F811
    client, account, template, clist = shop
    _campaign(account, template, clist, ["failed", "failed"])
    offer = MessageTemplate.objects.create(
        account=account,
        name="weekend_offer",
        whatsapp_template_name="weekend_offer",
        content="Hi {{1}}, 10% off this weekend",
        variables=["name"],
        category=MessageTemplate.Category.MARKETING,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    MessageTemplate.objects.create(
        account=account,
        name="login_code",
        whatsapp_template_name="login_code",
        content="Your code is {{1}}",
        category=MessageTemplate.Category.AUTHENTICATION,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    page = client.get(LIST).content.decode()
    assert "data-campaign-tiles" not in page
    card = page[page.index("data-campaign-start") :]
    card = card[: card.index("</section>")]
    assert "Your last campaign" in card and "App Download" in card
    assert "Finished: 0 of 2 sent, 2 failed" in card
    assert "Fix the payment method in Meta" in card and "data-campaign-fix" in card

    starts = card[card.index("data-campaign-starts") :]
    assert f'href="/campaigns/new/?template_id={offer.pk}"' in starts
    assert "Weekend offer" in starts and "Hi [name], 10% off this weekend" in starts
    assert f"?template_id={template.pk}" in starts  # a utility template counts too
    assert "Login code" not in starts


@_wa_urls
def test_a_starting_point_opens_the_composer_with_it_chosen(composer):  # noqa: F811
    client, account, template, clist = composer
    page = client.get(f"/campaigns/new/?template_id={template.pk}").content.decode()
    assert f"template: '{template.pk}'" in page


@_wa_urls
def test_with_no_approved_message_it_says_how_to_get_one(shop):  # noqa: F811
    client, account, template, clist = shop
    _campaign(account, template, clist, ["read"])
    template.approval_status = MessageTemplate.ApprovalStatus.PENDING
    template.save(update_fields=["approval_status"])
    card = client.get(LIST).content.decode()
    card = card[card.index("data-campaign-start") :]
    assert "data-campaign-starts-empty" in card and "/whatsapp/templates/new/" in card


@_wa_urls
def test_drafts_only_have_no_last_campaign(shop):  # noqa: F811
    client, account, template, clist = shop
    WhatsAppCampaign.objects.create(
        account=account, name="Idea", template=template, contact_list=clist
    )
    page = client.get(LIST).content.decode()
    card = page[page.index("data-campaign-start") :]
    card = card[: card.index("</section>")]
    assert "Your last campaign" not in card and "data-campaign-starts" in card


def test_recipients_without_a_message_are_left_out(shop):  # noqa: F811
    """A recipient skipped before a message existed has no delivery and no reply."""
    from apps.contacts.models import Contact

    _, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    WhatsAppCampaignRecipient.objects.create(
        campaign=campaign,
        contact=Contact.objects.create(account=account, phone="+260979999999"),
        status=WhatsAppCampaignRecipient.Status.SKIPPED,
    )
    assert reply_counts_for([campaign.pk]) == {campaign.pk: 0}
    assert (
        campaign_ui.rates(delivery_stats_for([campaign.pk])[campaign.pk])["prepared"]
        == 1
    )
