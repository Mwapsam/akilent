"""Campaigns plan, Phase H: chats and orders credited to a campaign, from evidence only.

The evidence is WhatsApp's own: a customer who swipes to reply to a campaign's message, or
taps one of its buttons, sends a message whose ``context.id`` names the campaign message.
That conversation gets a campaign entry point, and a lead or order made from it carries the
campaign (``ConversationAttribution.source_whatsapp_campaign``). A customer who just writes
after a campaign is a reply (Phase D), never a credit.
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.commerce.services import create_order, mark_order_paid
from apps.conversations.models import (
    Conversation,
    ConversationAttribution,
    ConversationEntryPoint,
)
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.campaigns import orders_for
from apps.whatsapp.interactive import replied_to
from apps.whatsapp.models import MessageLog
from apps.whatsapp.tests.test_campaign_results import (  # noqa: F401
    _campaign,
    _wa_urls,
    shop,
)

pytestmark = pytest.mark.django_db
LIST = "/campaigns/?channel=whatsapp"
_seq = iter(range(1, 10_000))


def _sent(campaign):
    """The campaign's first delivered message, given the id WhatsApp gave it."""
    log = (
        MessageLog.objects.filter(
            outbound_source__campaign_recipient__campaign=campaign
        )
        .order_by("pk")
        .first()
    )
    log.message_id = f"wamid.OUT{next(_seq)}"
    log.save(update_fields=["message_id"])
    return log


def _inbound(log, *, answers: str = "", text: str = "Yes please"):
    """The customer writes on the chat of ``log``; ``answers`` is the id their message
    replies to (a swipe-reply or a tapped button), "" for a message typed on its own."""
    wamid = f"wamid.IN{next(_seq)}"
    message = {"id": wamid, "type": "text", "text": {"body": text}}
    if answers:
        message["context"] = {"from": "260970000000", "id": answers}
    inbound = MessageLog.objects.create(
        account=log.account,
        conversation=log.conversation,
        contact=log.contact,
        direction=MessageLog.Direction.INBOUND,
        message_id=wamid,
        content=text,
        timestamp=timezone.now(),
        raw_payload={"entry": [{"changes": [{"value": {"messages": [message]}}]}]},
    )
    record_inbound_whatsapp_message(
        contact=log.contact.contact,
        wa_contact=log.contact,
        whatsapp_conversation=log.conversation,
        message_log=inbound,
        enroll_workflows=False,
    )
    return Conversation.get_or_create_for_whatsapp(log.conversation)


# --- The evidence ----------------------------------------------------------------------


def test_the_answered_message_is_read_from_the_webhook(shop):  # noqa: F811
    _, account, template, clist = shop
    log = _sent(_campaign(account, template, clist, ["read"]))
    _inbound(log, answers=log.message_id)
    inbound = MessageLog.objects.filter(direction=MessageLog.Direction.INBOUND).get()
    assert replied_to(inbound) == log.message_id
    inbound.raw_payload = {}
    assert replied_to(inbound) == ""


def test_a_reply_to_the_campaign_message_links_the_chat(shop):  # noqa: F811
    _, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    log = _sent(campaign)
    conversation = _inbound(log, answers=log.message_id)
    entry = ConversationEntryPoint.objects.get(conversation=conversation)
    assert entry.kind == ConversationEntryPoint.Kind.CAMPAIGN_REPLY
    assert entry.whatsapp_campaign == campaign and entry.post is None

    _inbound(log, answers=log.message_id, text="And one more")  # once per campaign
    assert ConversationEntryPoint.objects.filter(conversation=conversation).count() == 1


def test_a_message_typed_on_its_own_is_not_evidence(shop):  # noqa: F811
    _, account, template, clist = shop
    log = _sent(_campaign(account, template, clist, ["read"]))
    _inbound(log)  # minutes later, but not a reply to the campaign message
    assert not ConversationEntryPoint.objects.exists()


def test_a_reply_to_some_other_message_is_not_evidence(shop):  # noqa: F811
    _, account, template, clist = shop
    log = _sent(_campaign(account, template, clist, ["read"]))
    _inbound(log, answers="wamid.SOMETHING_ELSE")
    assert not ConversationEntryPoint.objects.exists()


def test_another_workspaces_campaign_is_never_credited(shop):  # noqa: F811
    from apps.accounts.models import Account
    from apps.whatsapp.models import MessageTemplate

    _, account, template, clist = shop
    mine = _sent(_campaign(account, template, clist, ["read"]))
    other = Account.objects.create(company_name="Other")
    theirs_tpl = MessageTemplate.objects.create(
        account=other,
        name="t",
        whatsapp_template_name="t",
        content="x",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    from apps.contacts.models import ContactList

    theirs = _sent(
        _campaign(
            other,
            theirs_tpl,
            ContactList.objects.create(account=other, name="L"),
            ["read"],
        )
    )
    _inbound(mine, answers=theirs.message_id)
    assert not ConversationEntryPoint.objects.exists()


def test_every_entry_point_names_a_post_or_a_campaign(shop):  # noqa: F811
    _, account, template, clist = shop
    log = _sent(_campaign(account, template, clist, ["read"]))
    conversation = _inbound(log)
    with pytest.raises(IntegrityError), transaction.atomic():
        ConversationEntryPoint.objects.create(
            account=account,
            conversation=conversation,
            kind=ConversationEntryPoint.Kind.CAMPAIGN_REPLY,
        )


# --- Orders ----------------------------------------------------------------------------


def _order(conversation, *, paid=False):
    order = create_order(
        conversation.account,
        conversation.contact,
        [{"name": "Dress", "unit_price": Decimal("25"), "quantity": 1}],
        conversation_id=conversation.public_id,
    )
    if paid:
        mark_order_paid(order, method="cash")
    return order


def test_an_order_from_that_chat_is_credited_to_the_campaign(shop):  # noqa: F811
    _, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    log = _sent(campaign)
    conversation = _inbound(log, answers=log.message_id)
    order = _order(conversation, paid=True)
    _order(conversation)

    attribution = ConversationAttribution.objects.get(order=order)
    assert attribution.source_whatsapp_campaign == campaign
    assert attribution.source_post is None
    assert orders_for([campaign.pk]) == {campaign.pk: {"orders": 2, "paid": 1}}


def test_an_order_from_a_chat_that_just_wrote_is_not_credited(shop):  # noqa: F811
    _, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    conversation = _inbound(_sent(campaign))
    _order(conversation, paid=True)
    assert orders_for([campaign.pk]) == {campaign.pk: {"orders": 0, "paid": 0}}


def test_a_guessed_conversation_credits_no_campaign(shop):  # noqa: F811
    """The database refuses a recent-conversation guess that names a campaign."""
    _, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    log = _sent(campaign)
    conversation = _inbound(log, answers=log.message_id)
    from apps.commerce.models import Order

    unattributed = Order.objects.create(account=account, contact=conversation.contact)
    with pytest.raises(IntegrityError), transaction.atomic():
        ConversationAttribution.objects.bulk_create(
            [
                ConversationAttribution(
                    account=account,
                    conversation=conversation,
                    channel=conversation.channel,
                    method=ConversationAttribution.Method.RECENT_CONVERSATION,
                    lead=None,
                    order=unattributed,
                    source_whatsapp_campaign=campaign,
                )
            ]
        )


# --- The list --------------------------------------------------------------------------


@_wa_urls
def test_the_list_shows_orders_by_campaign_and_this_month(shop):  # noqa: F811
    client, account, template, clist = shop
    for _ in range(2):
        _campaign(account, template, clist, ["read"])
    campaign = _campaign(account, template, clist, ["read"])
    log = _sent(campaign)
    conversation = _inbound(log, answers=log.message_id)
    _order(conversation, paid=True)
    _order(conversation)

    page = client.get(LIST).content.decode()
    head = page[page.index("<thead>") : page.index("</thead>")]
    assert ">Orders" in head and "swiped to reply" in head
    row = page[
        page.index(
            f'href="/campaigns/{campaign.pk}/" data-skeleton="campaign" class="font-medium'
        ) :
    ]
    row = row[: row.index("</tr>")]
    assert 'data-label="Orders" class="tabular-nums" title="1 paid">2<' in row

    tile = page[page.index('data-kpi="orders"') :]
    tile = tile[: tile.index('<div class="kpi"')]
    assert 'kpi-value tabular-nums">2<' in tile and "1 paid" in tile


# --- Content Studio's report keeps its meaning -----------------------------------------


def test_content_studio_doesnt_count_a_campaign_chat_as_a_posts(shop):  # noqa: F811
    from apps.conversations import reporting
    from apps.publishing.models import ContentPost, TrackedLink
    from apps.publishing.results import journey

    _, account, template, clist = shop
    log = _sent(_campaign(account, template, clist, ["read"]))
    _inbound(log, answers=log.message_id)

    # A campaign's entry point alone doesn't switch on Content Studio's chat tracking.
    j = journey(account, reporting.last_days(30))
    assert j is None or j["chats"] is None

    # With a post's link (tracking on), the chat is one no post is credited with.
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type="image",
        caption="New stock in!",
        status=ContentPost.Status.PUBLISHED,
    )
    TrackedLink.objects.create(
        account=account, post=post, code="ABC123", wa_number="260971111111"
    )
    j = journey(account, reporting.last_days(30))
    assert (j["chats"], j["untracked_chats"]) == (0, 1)


@_wa_urls
def test_the_order_says_which_campaign_it_came_from(shop):  # noqa: F811
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["read"])
    log = _sent(campaign)
    order = _order(_inbound(log, answers=log.message_id))
    html = client.get(f"/orders/{order.public_id}/").content.decode()
    assert 'data-attribution="campaign"' in html
    assert "From a WhatsApp campaign:" in html and campaign.name in html
    assert f'href="/campaigns/{campaign.pk}/"' in html
