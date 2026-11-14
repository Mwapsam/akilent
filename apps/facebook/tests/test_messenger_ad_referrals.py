"""Click-to-Messenger ads (docs/strategy/akilent-ads.md, Messenger added 14 November 2026).

A Messenger chat that came from a Meta ad is stored and counted as paid, exactly like
WhatsApp and Instagram: it never counts as a post's organic chat, an ad Akilent can't match
to a post is still paid, and the ad automations answer it. A thread reopened from an ad
(the standalone ``messaging_referrals`` event) is stored against the existing chat and never
creates one.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.automation.models import Workflow, WorkflowRun
from apps.conversations import ad_referrals
from apps.conversations.models import (
    AdReferral,
    Conversation,
    ConversationEntryPoint,
)
from apps.facebook.models import FacebookMessage
from apps.facebook.services.pages import WEBHOOK_FIELDS

from .helpers import APP_SECRET, FakeGraph, dm_payload, make_page, post_webhook

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_signed", "graph")]

AD = {
    "ref": "",
    "source": "ADS",
    "type": "OPEN_THREAD",
    "ad_id": "120200000000099",
    "ads_context_data": {
        "ad_title": "Winter coats",
        "photo_url": "https://example.com/c.jpg",
        "post_id": "555_777",
    },
}


@pytest.fixture
def _signed():
    with override_settings(
        WHATSAPP_APP_SECRET=APP_SECRET,
        INSTAGRAM_APP_SECRET="",
        FACEBOOK_VERIFY_TOKEN="fb-verify",
    ):
        yield


@pytest.fixture
def graph():
    fake = FakeGraph()
    with patch("apps.facebook.providers.meta.requests.request", side_effect=fake):
        yield fake


def _ad_message(conn, psid, text, *, mid, referral=AD, on_message=False):
    """A Messenger message that came from a Click-to-Messenger ad. Meta puts the referral
    on the messaging entry; accept it on the message too, as Instagram does."""
    payload = dm_payload(conn.page_id, psid, text, mid=mid)
    item = payload["entry"][0]["messaging"][0]
    if on_message:
        item["message"]["referral"] = referral
    else:
        item["referral"] = referral
    return payload


def _reopened(conn, psid, referral=AD):
    """The standalone event: the customer reopened the thread from an ad, no message."""
    payload = dm_payload(conn.page_id, psid, "x")
    item = payload["entry"][0]["messaging"][0]
    del item["message"]
    item["referral"] = referral
    return payload


# --- Parsing -------------------------------------------------------------------------------


def test_a_messenger_ad_referral_is_normalised():
    data = ad_referrals.from_messenger(AD)
    assert (data["source_type"], data["source_id"], data["headline"]) == (
        "ad",
        "120200000000099",
        "Winter coats",
    )
    assert data["post_ids"] == ["555_777"] and data["media_type"] == "image"


def test_an_m_me_link_ref_is_not_an_ad():
    assert ad_referrals.from_messenger({"ref": "spring", "source": "SHORTLINK"}) is None


# --- Capture through the real webhook ---------------------------------------------------------


@pytest.mark.parametrize("on_message", [False, True])
def test_a_chat_from_an_ad_is_stored_as_paid(client, on_message):
    account, _user, conn = make_page()
    payload = _ad_message(conn, "psid-ad", "Still in stock?", mid="m_ad", on_message=on_message)

    post_webhook(client, payload)
    post_webhook(client, payload)  # Meta redelivers

    [ref] = AdReferral.objects.filter(account=account)
    assert ref.channel == AdReferral.Channel.FACEBOOK
    assert ref.get_channel_display() == "Messenger"
    assert (ref.source_id, ref.headline) == ("120200000000099", "Winter coats")
    assert ref.message.facebook_message == FacebookMessage.objects.get(message_id="m_ad")
    [entry] = ConversationEntryPoint.objects.filter(
        conversation=ref.conversation, kind=ConversationEntryPoint.Kind.AD_REFERRAL
    )
    assert entry.post is None  # paid, never a post's organic chat


def test_a_plain_messenger_chat_has_no_ad(client):
    account, _user, conn = make_page()
    post_webhook(client, dm_payload(conn.page_id, "psid-1", "Hello", mid="m_1"))
    assert not AdReferral.objects.filter(account=account).exists()


def test_an_ad_for_a_studio_post_names_it_only_as_context(client):
    from apps.publishing.models import ContentPost

    account, _user, conn = make_page()
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type="image",
        caption="Coats",
        status=ContentPost.Status.PUBLISHED,
        fb_post_id="555_777",
    )
    post_webhook(client, _ad_message(conn, "psid-ad", "Hi", mid="m_ad"))
    ref = AdReferral.objects.get(account=account)
    assert ref.post == post
    assert ConversationEntryPoint.objects.get(ad_referral=ref).post is None


def test_a_reopened_thread_is_stored_against_the_existing_chat(client):
    account, _user, conn = make_page()
    post_webhook(client, dm_payload(conn.page_id, "psid-1", "Hello", mid="m_1"))
    conversation = Conversation.objects.get(account=account)

    post_webhook(client, _reopened(conn, "psid-1"))

    ref = AdReferral.objects.get(account=account)
    assert (ref.conversation, ref.message) == (conversation, None)
    assert Conversation.objects.filter(account=account).count() == 1


def test_a_reopen_from_a_stranger_creates_nothing(client):
    account, _user, conn = make_page()
    post_webhook(client, _reopened(conn, "psid-new"))
    assert not Conversation.objects.filter(account=account).exists()
    assert not AdReferral.objects.exists()


def test_another_page_cannot_file_an_ad_under_this_workspace(client):
    account, _user, conn = make_page()
    other, _u, other_conn = make_page()
    post_webhook(client, dm_payload(conn.page_id, "psid-1", "Hello", mid="m_1"))
    # The same PSID string reopening a thread on the other Page.
    post_webhook(client, _reopened(other_conn, "psid-1"))
    assert not AdReferral.objects.filter(account=account).exists()


# --- Backfill --------------------------------------------------------------------------------


def test_backfill_recovers_ads_from_stored_messenger_messages(client):
    account, _user, conn = make_page()
    post_webhook(client, _ad_message(conn, "psid-ad", "Hi", mid="m_ad"))
    AdReferral.objects.all().delete()  # as if it arrived before capture existed

    out = StringIO()
    call_command("ad_referrals_backfill", "--account", account.slug, stdout=out)
    call_command("ad_referrals_backfill", "--account", account.slug, stdout=StringIO())

    assert "stored: 1 new" in out.getvalue()
    assert AdReferral.objects.get(account=account).channel == AdReferral.Channel.FACEBOOK


# --- Ad automations answer Messenger ad chats ---------------------------------------------------


def test_the_ad_automation_answers_a_messenger_ad_chat(client, settings):
    from apps.core.models import SiteSettings

    settings.RELEASED_FEATURES = frozenset({"ads_automation"})
    site = SiteSettings.load()
    site.automation_events_enabled = True
    site.save()
    from django.core.cache import cache

    from apps.whatsapp.tasks import _AUTOMATION_EVENTS_CACHE_KEY

    cache.delete(_AUTOMATION_EVENTS_CACHE_KEY)
    account, _user, conn = make_page()
    wf = Workflow.objects.create(
        account=account,
        name="Ad reply",
        slug="ad-reply",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "conversation.message_received", "from_ad": True},
            "steps": [
                {"id": "tag", "type": "add_tag", "tag": "from-ad", "next": "stop"},
                {"id": "stop", "type": "stop"},
            ],
        },
    )
    post_webhook(client, _ad_message(conn, "psid-ad", "Hi", mid="m_ad"))
    post_webhook(client, dm_payload(conn.page_id, "psid-ad", "Size M?", mid="m_2"))
    assert WorkflowRun.objects.filter(workflow=wf).count() == 1


# --- Webhook subscription ----------------------------------------------------------------------


def test_pages_subscribe_to_referral_events():
    assert "messaging_referrals" in WEBHOOK_FIELDS.split(",")


def test_resubscribe_command_subscribes_active_pages(graph):
    account, _user, conn = make_page()
    out = StringIO()
    with patch(
        "apps.facebook.services.pages.subscribe_page", return_value=True
    ) as subscribe:
        call_command("facebook_resubscribe", stdout=out)
    subscribe.assert_called_once()
    assert subscribe.call_args.args[0] == conn
    assert "subscribed: 1; failed: 0" in out.getvalue()

    out = StringIO()
    with patch("apps.facebook.services.pages.subscribe_page") as subscribe:
        call_command("facebook_resubscribe", "--dry-run", stdout=out)
    subscribe.assert_not_called()
    assert "would subscribe" in out.getvalue()
