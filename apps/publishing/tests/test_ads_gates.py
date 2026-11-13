"""Akilent Ads, the parts that need no new Meta permissions (docs/strategy/akilent-ads.md).

Gate 2 (``cs_promote_candidates``): posts worth promoting, labelled by the strongest floor
their organic evidence clears. Gate 3 (``ads_inbox_verdict``): what happened to each Meta
ad's chats. Gate 0: ``ads_evidence_report``. Paid chats never count for a post.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.accounts.business_hours import open_seconds_between, save_hours
from apps.conversations import ad_referrals, ad_verdict, reporting
from apps.conversations.models import AdReferral, Message
from apps.publishing import promote, sales
from apps.publishing.tests.test_sales_report import (  # noqa: F401 (fixtures)
    Shop,
    _channels,
    _no_workflows,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def shop():
    return Shop()


def _ad_chat(shop, source_id="120200000000001", headline="Summer sale", at=None):
    """A chat that came from a Meta ad, through the real capture path."""
    conversation, contact = shop.chat(None, at=at)
    message = conversation.messages.get()
    ad_referrals.record(
        conversation,
        message,
        AdReferral.Channel.WHATSAPP,
        ad_referrals.from_whatsapp(
            {"source_id": source_id, "source_type": "ad", "headline": headline}
        ),
    )
    return conversation, contact


def _reply(conversation, *, after, sent_by="", auto_kind="", status="sent"):
    meta = {}
    if sent_by:
        meta["sent_by"] = sent_by
    if auto_kind:
        meta["auto_kind"] = auto_kind
    return Message.objects.create(
        account=conversation.account,
        conversation=conversation,
        direction=Message.Direction.OUTBOUND,
        body="Hello!",
        timestamp=conversation.messages.order_by("timestamp").first().timestamp + after,
        status=status,
        metadata=meta,
    )


# --- Gate 2: labels and floors -----------------------------------------------------------


@pytest.mark.parametrize(
    "counts,label",
    [
        ({"paid_orders": 3, "leads": 0, "chats": 3}, "sold"),
        ({"paid_orders": 1, "leads": 5, "chats": 9}, "leads"),  # one order isn't "sold"
        ({"paid_orders": 2, "leads": 2, "chats": 3}, "chats"),
        ({"paid_orders": 2, "leads": 2, "chats": 2}, None),
    ],
)
def test_the_strongest_floor_cleared_wins(counts, label):
    assert promote.label_for(counts) == label


def test_candidates_rank_sold_before_leads_before_chats(shop):
    sold, leads, chats, thin = (shop.post(n) for n in ("Sold", "Leads", "Chats", "Thin"))
    for c in shop.chats(sold, 3):
        shop.order(c, "20.00", "ZMW")
    for c in shop.chats(leads, 4):
        shop.lead(c)
    shop.chats(chats, 6)
    shop.chats(thin, 2)

    data = promote.candidates(shop.account)

    assert data["enough"] is True
    assert [(c["post"], c["label"]) for c in data["candidates"]] == [
        (sold, "sold"),
        (leads, "leads"),
        (chats, "chats"),
    ]
    assert data["candidates"][0]["revenue"] == [
        {"currency": "ZMW", "total": Decimal("60.00")}
    ]


def test_no_candidate_below_every_floor(shop):
    shop.chats(shop.post("Quiet"), 2)
    data = promote.candidates(shop.account)
    assert data["enough"] is False and data["candidates"] == []


def test_ad_chats_never_make_a_post_a_candidate(shop):
    post = shop.post("Boosted")
    for _ in range(4):
        conversation, _ = _ad_chat(shop)
        # Even with the post's ref-code entry too, the chat is paid.
        from apps.conversations.models import ConversationEntryPoint

        ConversationEntryPoint.objects.create(
            account=shop.account,
            conversation=conversation,
            kind="tracked_link",
            post=post,
            variant=post.variants.get(),
        )
    assert promote.candidates(shop.account)["enough"] is False
    assert sales.report(shop.account, reporting.last_days(30))["from_ads"]["chats"] == 4


# --- Gate 3: the verdict clock -----------------------------------------------------------


def test_open_seconds_counts_only_opening_hours(shop):
    hours = save_hours(
        shop.account,
        tz="UTC",
        schedule={"mon": {"open": "09:00", "close": "17:00"}},
    )
    # Monday 16:30 to Tuesday 10:00: only 30 open minutes (closed all Tuesday).
    start = datetime(2026, 11, 9, 16, 30, tzinfo=UTC)
    end = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
    assert open_seconds_between(hours, start, end) == 30 * 60
    assert open_seconds_between(None, start, end) == (end - start).total_seconds()


def test_a_holding_reply_does_not_stop_the_clock(shop):
    conversation, _ = _ad_chat(shop, at=timezone.now() - timedelta(hours=5))
    _reply(conversation, after=timedelta(minutes=1), sent_by="ai", auto_kind="holding")
    _reply(conversation, after=timedelta(minutes=2), sent_by="system")
    _reply(conversation, after=timedelta(hours=2))  # the person's real answer

    [row] = ad_verdict.report(shop.account, reporting.last_days(30))

    assert (row["chats"], row["replied"], row["replied_by_person"]) == (1, 1, 1)
    assert row["waited_too_long"] == 1
    assert row["median_total_seconds"] == 2 * 3600


def test_an_automation_that_answers_counts(shop):
    conversation, _ = _ad_chat(shop)
    _reply(conversation, after=timedelta(seconds=30), sent_by="automation")

    [row] = ad_verdict.report(shop.account, reporting.last_days(30))

    assert (row["replied_automatically"], row["waited_too_long"]) == (1, 0)


def test_a_chat_never_answered_waits_too_long_only_after_the_threshold(shop):
    _ad_chat(shop, source_id="old", at=timezone.now() - timedelta(hours=3))
    _ad_chat(shop, source_id="new", at=timezone.now() - timedelta(minutes=10))

    rows = {r["source_id"]: r for r in ad_verdict.report(shop.account, reporting.last_days(30))}

    assert (rows["old"]["never_replied"], rows["old"]["waited_too_long"]) == (1, 1)
    assert (rows["new"]["never_replied"], rows["new"]["waited_too_long"]) == (1, 0)


def test_failed_sends_are_not_replies(shop):
    conversation, _ = _ad_chat(shop, at=timezone.now() - timedelta(hours=3))
    _reply(conversation, after=timedelta(minutes=1), status="failed")
    [row] = ad_verdict.report(shop.account, reporting.last_days(30))
    assert row["replied"] == 0


def test_leads_and_paid_orders_per_ad_per_currency(shop):
    a1 = _ad_chat(shop, source_id="A")
    a2 = _ad_chat(shop, source_id="A")
    b1 = _ad_chat(shop, source_id="B")
    shop.lead(a1)
    shop.order(a1, "10.00", "USD")
    shop.order(a2, "50.00", "ZMW")
    shop.order(b1, "5.00", "USD", paid=False)

    rows = {r["source_id"]: r for r in ad_verdict.report(shop.account, reporting.last_days(30))}

    assert (rows["A"]["chats"], rows["A"]["leads"], rows["A"]["paid_orders"]) == (2, 1, 2)
    assert rows["A"]["revenue"] == [
        {"currency": "USD", "total": Decimal("10.00")},
        {"currency": "ZMW", "total": Decimal("50.00")},
    ]
    assert (rows["B"]["orders"], rows["B"]["paid_orders"]) == (1, 0)


def test_another_workspace_ads_are_not_reported(shop):
    _ad_chat(Shop("other"))
    assert ad_verdict.report(shop.account, reporting.last_days(30)) == []


# --- Pages, behind their flags ------------------------------------------------------------


def test_sales_page_shows_promote_and_ads_only_when_released(shop, settings):
    post = shop.post("Sandals")
    for c in shop.chats(post, 3):
        shop.order(c, "12.00", "ZMW")
    _ad_chat(shop, headline="Big ad")

    settings.RELEASED_FEATURES = frozenset({sales.FLAG})
    html = shop.client.get(reverse("publishing:sales")).content.decode()
    assert "data-promote" not in html and "data-ad-verdict" not in html
    # Ad chats are paid, so their totals line shows whatever the flags say.
    assert 'data-sales-total="ads"' in html

    settings.RELEASED_FEATURES = frozenset(
        {sales.FLAG, promote.FLAG, ad_verdict.FLAG}
    )
    html = shop.client.get(reverse("publishing:sales")).content.decode()
    assert f'data-promote-post="{post.pk}"' in html
    assert 'data-promote-label="sold"' in html
    assert 'data-ad="120200000000001"' in html and "Big ad" in html


def test_post_page_shows_the_promote_card_when_released(shop, settings):
    post = shop.post("Sandals")
    shop.chats(post, 3)
    settings.RELEASED_FEATURES = frozenset({promote.FLAG})
    html = shop.client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()
    assert "data-promote-card" in html and 'data-promote-label="chats"' in html

    settings.RELEASED_FEATURES = frozenset()
    html = shop.client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()
    assert "data-promote-card" not in html


# --- Gate 0 ------------------------------------------------------------------------------


def test_evidence_report_counts_shares_of_all_active_workspaces(shop):
    for c in shop.chats(shop.post("Sandals"), 3):
        shop.order(c)
    Shop("quiet")
    _ad_chat(shop)

    out = StringIO()
    call_command("ads_evidence_report", stdout=out)
    data = json.loads(out.getvalue())

    assert data["active_workspaces"] == 2
    assert data["above_floor"]["sold"] == {"count": 1, "share_pct": 50.0}
    assert data["above_floor"]["chats"]["count"] == 1
    assert data["ad_referrals"]["stored"] == 1
    assert data["stop_condition"]["stop"] is False
    assert data["product_availability"].startswith("not verifiable")
