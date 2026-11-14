"""The "ad chats waiting" alert (``ad_alerts``, release flag ``ads_automation``).

An open chat from a Meta ad with no qualifying reply after an hour of business time: the
owners get one email per workspace per sweep, once per chat. Holding replies and system
messages don't count as an answer, just as in the ad verdict.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.business_hours import save_hours
from apps.conversations import ad_alerts, reporting
from apps.conversations.models import Conversation
from apps.publishing.tests.test_ads_gates import _ad_chat, _reply
from apps.publishing.tests.test_sales_report import (  # noqa: F401 (fixtures)
    Shop,
    _channels,
    _no_workflows,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def shop():
    return Shop()


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({ad_alerts.FLAG})


@pytest.fixture
def sent():
    """Each team email the sweep sends: ``(account, kwargs)``."""
    calls = []

    def fake(account, **kwargs):
        calls.append((account, kwargs))
        return 1

    with patch("apps.accounts.notifications.notify_team", side_effect=fake):
        yield calls


def _ago(**kw):
    return timezone.now() - timedelta(**kw)


def test_nothing_happens_until_released(shop, settings, sent):
    settings.RELEASED_FEATURES = frozenset()
    _ad_chat(shop, at=_ago(hours=3))
    assert ad_alerts.sweep()["skipped"] is True
    assert sent == []


def test_one_email_per_workspace_listing_every_waiting_chat(shop, released, sent):
    for n in range(3):
        _ad_chat(shop, source_id=f"ad{n}", headline=f"Sale {n}", at=_ago(hours=2))
    _ad_chat(shop, source_id="fresh", at=_ago(minutes=10))  # not an hour yet

    result = ad_alerts.sweep()

    assert result == {"chats": 3, "emails": 1}
    [(account, email)] = sent
    assert account == shop.account and email["to"] == "owners"
    assert email["subject"] == "3 chats from your Meta ads have waited over 1 business hour"
    assert "Sale 0" in email["text"] and "Sale 2" in email["text"]
    assert email["path"].endswith("from=ad")


def test_a_chat_is_alerted_once(shop, released, sent):
    _ad_chat(shop, at=_ago(hours=2))
    ad_alerts.sweep()
    assert ad_alerts.sweep() == {"chats": 0, "emails": 0}
    assert len(sent) == 1


def test_a_holding_reply_is_not_an_answer_but_a_real_one_is(shop, released):
    held, _ = _ad_chat(shop, source_id="held", at=_ago(hours=2))
    _reply(held, after=timedelta(minutes=1), sent_by="ai", auto_kind="holding")
    answered, _ = _ad_chat(shop, source_id="answered", at=_ago(hours=2))
    _reply(answered, after=timedelta(minutes=5))

    waiting = ad_alerts.waiting()[shop.account.id]

    assert [ref.source_id for _c, ref, _s in waiting] == ["held"]


def test_closed_hours_dont_count(shop, released):
    # Open only on a day three days from now: no business time has passed since.
    day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[
        (timezone.now().weekday() + 3) % 7
    ]
    save_hours(shop.account, tz="UTC", schedule={day: {"open": "09:00", "close": "17:00"}})
    _ad_chat(shop, at=_ago(hours=5))
    assert ad_alerts.waiting() == {}


def test_closed_chats_and_old_ads_are_left_alone(shop, released):
    closed, _ = _ad_chat(shop, source_id="closed", at=_ago(hours=2))
    Conversation.objects.filter(pk=closed.pk).update(status=Conversation.Status.CLOSED)
    _ad_chat(shop, source_id="old", at=_ago(days=2))
    assert ad_alerts.waiting() == {}


def test_long_digests_are_cut_short(shop, released, sent):
    for n in range(ad_alerts.DIGEST_LIMIT + 2):
        _ad_chat(shop, source_id=f"ad{n}", at=_ago(hours=2))
    ad_alerts.sweep()
    [(_account, email)] = sent
    assert "and 2 more" in email["text"]
    assert len(email["text"]) <= 1000  # notify_team's limit


def test_each_workspace_hears_only_about_its_own(shop, released, sent):
    other = Shop("other")
    _ad_chat(shop, source_id="mine", headline="Mine", at=_ago(hours=2))
    _ad_chat(other, source_id="theirs", headline="Theirs", at=_ago(hours=2))

    assert ad_alerts.sweep() == {"chats": 2, "emails": 2}
    by_account = {account.pk: email["text"] for account, email in sent}
    assert "Mine" in by_account[shop.account.pk]
    assert "Theirs" not in by_account[shop.account.pk]
    assert "Theirs" in by_account[other.account.pk]


def test_what_needs_you_lists_waiting_ad_chats(shop, released):
    _ad_chat(shop, at=_ago(hours=2))
    risk = reporting.at_risk(shop.account)
    assert risk["counts"]["ad_waiting"] == 1
    [action] = [a for a in risk["actions"] if a["key"] == "ad_waiting"]
    assert action["url"].endswith("from=ad")


def test_inbox_filter_shows_only_ad_chats(shop, released):
    ad_conversation, _ = _ad_chat(shop, at=_ago(hours=2))
    organic, _ = shop.chat(None, at=_ago(hours=2))
    html = shop.client.get(reverse("conversations:inbox") + "?from=ad").content.decode()
    assert str(ad_conversation.public_id) in html
    assert str(organic.public_id) not in html
