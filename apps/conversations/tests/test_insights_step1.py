"""Insights step 1 (2026-11-08): numbers that agree, honest small samples, one period, and a
quiet refresh.

- one rule for answered / missed (reporting._outcome): "not answered within 24 hours" and
  "missed" are the same number, and an enquiry still inside its 24 hours is neither;
- below reporting.MIN_SAMPLE a rate reads "1 of 1", never "100%", and no change is shown;
- the headline follows the page's period; "What needs you" says it's right now;
- the body re-reads itself in the background and comes back alone for that request.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import reporting
from apps.conversations.models import Conversation, Message
from apps.conversations.templatetags.insights_tags import share, thin

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND
NOW = timezone.now()


@pytest.fixture
def shop(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _enquiry(account, at, reply_after=None, sent_by=""):
    contact = Contact.objects.create(
        account=account, phone=f"+26097{Contact.objects.count():07d}"
    )
    conv = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    Message.objects.create(
        account=account, conversation=conv, direction=IN, body="Hi", timestamp=at
    )
    if reply_after is not None:
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="Hello",
            status="sent",
            timestamp=at + reply_after,
            metadata={"sent_by": sent_by} if sent_by else {},
        )
    return conv


# --- One source -----------------------------------------------------------------------


def test_unanswered_and_missed_are_one_number_and_an_hour_old_message_is_neither(shop):
    _, account = shop
    _enquiry(account, NOW - timedelta(days=3))  # missed, never answered
    _enquiry(account, NOW - timedelta(days=3), reply_after=timedelta(hours=30))  # late
    _enquiry(account, NOW - timedelta(days=3), reply_after=timedelta(minutes=4))
    _enquiry(account, NOW - timedelta(hours=1))  # still inside its 24 hours
    period = reporting.last_days(30, NOW)

    resp = reporting.response(account, period, now=NOW)
    rec = reporting.recovery(account, period, now=NOW)
    assert resp["enquiries"] == 4 and resp["pending"] == 1 and resp["settled"] == 3
    assert resp["answered"] == 1 and resp["unanswered"] == resp["missed"] == 2
    assert rec["missed"] == resp["missed"]
    assert rec["recovered"] == 1  # the late reply


def test_the_median_says_how_many_first_replies_were_automatic(shop):
    _, account = shop
    _enquiry(account, NOW - timedelta(days=2), timedelta(seconds=4), sent_by="ai")
    _enquiry(account, NOW - timedelta(days=2), timedelta(hours=2))
    resp = reporting.response(account, reporting.last_days(30, NOW), now=NOW)
    assert resp["replied"] == 2 and resp["automatic_first_replies"] == 1


# --- Small samples --------------------------------------------------------------------


def test_a_rate_from_a_handful_reads_as_a_count():
    assert share(1, 1) == "1 of 1"
    assert share(0, 0) == "—"
    assert share(4, 5) == "80%"
    assert thin(1) and not thin(0) and not thin(reporting.MIN_SAMPLE)


def test_no_change_is_shown_from_too_few_replies(shop):
    _, account = shop
    _enquiry(account, NOW - timedelta(days=2), timedelta(minutes=1))
    _enquiry(account, NOW - timedelta(days=9), timedelta(hours=5))
    proof = reporting.proof(account, reporting.last_days(7, NOW), now=NOW)
    assert proof["median"]["seconds"] == 60
    assert proof["median"]["change"] is None


# --- One period, and the page ---------------------------------------------------------


def test_the_page_uses_one_period_and_says_what_is_right_now(shop):
    client, account = shop
    _enquiry(account, NOW - timedelta(days=3), reply_after=timedelta(minutes=4))
    page = client.get("/insights/?period=7").content.decode()
    assert "In the last 7 days, 1 customer got a reply." in page
    assert "This week, Akilent" not in page and "vs last week" not in page
    assert '<option value="7" selected>' in " ".join(page.split())
    assert (
        'What needs you <span class="font-normal text-ink-subtle">· right now</span>'
        in page
    )
    # One answered enquiry: a count, not "100%".
    response = page[page.index('id="response-h"') :]
    assert "1 of 1" in response and "100%" not in response
    assert "Not enough data yet for a rate" in response


def test_the_body_refreshes_quietly_and_comes_back_alone(shop):
    client, _ = shop
    page = client.get("/insights/").content.decode()
    body = page[page.index('<div id="insights-body"') :]
    tag = body[: body.index(">")]
    assert "every 300s" in tag and "visibilitychange" in tag
    assert 'hx-disinherit="*"' in tag and "data-auto-refresh" in tag

    partial = client.get("/insights/", HTTP_HX_REQUEST="true").content.decode()
    assert partial.lstrip().startswith('<div id="insights-body"')
    # The body alone: no page title (the period picker is part of the body, so a tab
    # switch or a refresh keeps it in step).
    assert "<html" not in partial and "<h1" not in partial
    assert 'id="period"' in partial
