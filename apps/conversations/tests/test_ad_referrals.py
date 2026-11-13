"""Meta ad referrals: stored, and paid takes precedence (docs/strategy/akilent-ads.md §4).

A chat that came from a Click-to-WhatsApp or Click-to-Instagram-Direct ad is paid. It is
never counted as a post's organic chat, even when the ad promotes that post or the message
also carries the post's ref code. An ad Akilent can't match to a post is still paid.
"""

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import Account
from apps.commerce.services import create_order, mark_order_paid
from apps.contacts.models import Contact
from apps.conversations import ad_referrals, reporting
from apps.conversations.models import AdReferral, ConversationEntryPoint, Message
from apps.conversations.services import record_inbound_whatsapp_message
from apps.publishing.links import link_for_variant
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.results import journey, post_credit_q, results_for
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppBusinessNumber, WhatsAppContact

pytestmark = pytest.mark.django_db

WA_AD = {
    "source_url": "https://fb.me/abc",
    "source_id": "120200000000001",
    "source_type": "ad",
    "headline": "Summer dresses",
    "body": "Tap to chat",
    "media_type": "image",
    "image_url": "https://example.com/i.jpg",
    "ctwa_clid": "ARAkLkA8rmlFeiCktEJQ",
}


def _account(slug="mwamba"):
    account = Account.objects.create(company_name=slug.title(), slug=slug)
    WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id=f"pn-{slug}",
        display_number="+260 97 1111111",
        waba_id=f"waba-{slug}",
        access_token="tok",
        registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
    )
    return account


def _post(account, fb_post_id="", ig_media_id=""):
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type="image",
        caption="New stock in!",
        status=ContentPost.Status.PUBLISHED,
        fb_post_id=fb_post_id,
        ig_media_id=ig_media_id,
    )
    ContentPostVariant.objects.create(
        post=post,
        platform="facebook",
        caption="New stock in!",
        status=ContentPostVariant.Status.PUBLISHED,
        publication_id="fb_media_1",
    )
    return post


@pytest.fixture
def account():
    return _account()


@pytest.fixture
def deliver(account):
    """Deliver an inbound WhatsApp message through the real path, with its webhook payload."""
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    counter = iter(range(1, 100))

    def _deliver(body, referral=None):
        mid = f"wamid.{next(counter)}"
        item = {"id": mid, "from": "260971234567", "type": "text", "text": {"body": body}}
        if referral is not None:
            item["referral"] = referral
        payload = {"entry": [{"changes": [{"value": {"messages": [item]}}]}]}
        log = MessageLog.objects.create(
            account=account,
            conversation=wa_conversation,
            contact=wa_contact,
            message_id=mid,
            direction=MessageLog.Direction.INBOUND,
            message_type=MessageLog.MessageType.TEXT,
            content=body,
            status=MessageLog.Status.DELIVERED,
            timestamp=timezone.now(),
            raw_payload=payload,
        )
        return record_inbound_whatsapp_message(
            contact=contact,
            wa_contact=wa_contact,
            whatsapp_conversation=wa_conversation,
            message_log=log,
        )

    return contact, _deliver


# --- Parsing ---------------------------------------------------------------------


def test_whatsapp_referral_is_normalised():
    data = ad_referrals.from_whatsapp(WA_AD)
    assert data["source_id"] == "120200000000001"
    assert data["source_type"] == "ad"
    assert data["headline"] == "Summer dresses"
    assert data["ctwa_clid"] == "ARAkLkA8rmlFeiCktEJQ"
    assert data["raw"] == WA_AD


def test_instagram_ad_referral_is_normalised():
    data = ad_referrals.from_instagram(
        {
            "ref": "x",
            "ad_id": "6000000001",
            "source": "ADS",
            "type": "OPEN_THREAD",
            "ads_context_data": {
                "ad_title": "Shoes sale",
                "photo_url": "https://example.com/p.jpg",
                "post_id": "17890000000",
            },
        }
    )
    assert data["source_id"] == "6000000001"
    assert data["headline"] == "Shoes sale"
    assert data["media_type"] == "image"
    assert data["post_ids"] == ["17890000000"]


@pytest.mark.parametrize(
    "referral",
    [None, {}, {"source_type": "ad"}, "nonsense", {"source_id": ""}],
)
def test_no_ad_id_means_no_referral(referral):
    assert ad_referrals.from_whatsapp(referral) is None


def test_instagram_non_ad_referral_is_ignored():
    assert ad_referrals.from_instagram({"source": "SHORTLINK", "ref": "x"}) is None


# --- Capture and precedence ------------------------------------------------------


def test_a_chat_from_an_ad_is_stored_and_paid(account, deliver):
    contact, send = deliver
    conversation = send("Hi, is this available?", referral=WA_AD)

    ref = AdReferral.objects.get(conversation=conversation)
    assert (ref.channel, ref.source_id, ref.headline) == (
        "whatsapp",
        WA_AD["source_id"],
        "Summer dresses",
    )
    assert ref.post is None  # an ad id alone can't name a post
    entry = ConversationEntryPoint.objects.get(conversation=conversation)
    assert entry.kind == ConversationEntryPoint.Kind.AD_REFERRAL
    assert entry.post is None and entry.ad_referral == ref


def test_capture_is_idempotent(account, deliver):
    _, send = deliver
    conversation = send("Hi", referral=WA_AD)
    send("Hi again", referral=WA_AD)  # Meta can repeat the referral
    assert AdReferral.objects.filter(conversation=conversation).count() == 1
    assert ConversationEntryPoint.objects.filter(conversation=conversation).count() == 1


def test_ad_beats_a_ref_code_and_never_credits_the_post(account, deliver):
    """An ad whose prefilled text carries a post's ref code: paid, not organic."""
    contact, send = deliver
    post = _post(account)
    variant = post.variants.get()
    link = link_for_variant(variant)

    conversation = send(f"How much? (ref {link.code})", referral=WA_AD)
    kinds = set(
        ConversationEntryPoint.objects.filter(conversation=conversation).values_list(
            "kind", flat=True
        )
    )
    assert kinds == {"ad_referral", "tracked_link"}

    order = create_order(
        account,
        contact,
        [{"name": "Dress", "unit_price": Decimal("120.00"), "quantity": 1}],
        currency="ZMW",
        conversation_id=conversation.public_id,
    )
    mark_order_paid(order, method="cash")
    order.refresh_from_db()
    assert order.attribution.entry_point.kind == "ad_referral"
    assert order.attribution.source_post is None
    assert not type(order).objects.filter(post_credit_q(), pk=order.pk).exists()

    # The post's card and the journey count it as paid, never as the post's chat.
    [row] = results_for(post)
    assert (row["chats"], row["orders"]) == (0, 0)
    j = journey(account, reporting.last_days(30))
    assert (j["chats"], j["ad_chats"], j["untracked_chats"]) == (0, 1, 0)
    assert [(r["currency"], r["total"]) for r in j["ad_revenue"]] == [
        ("ZMW", Decimal("120.00"))
    ]
    assert j["untracked_revenue"] == []


def test_an_ad_promoting_a_studio_post_is_matched_but_still_paid(account, deliver):
    _, send = deliver
    post = _post(account, fb_post_id="111_222")
    boosted = {**WA_AD, "source_type": "post", "source_id": "222"}
    conversation = send("Hi", referral=boosted)

    ref = AdReferral.objects.get(conversation=conversation)
    assert ref.post == post  # context: the ad promotes this post
    entry = ConversationEntryPoint.objects.get(conversation=conversation)
    assert entry.post is None  # but the chat is paid, not the post's organic chat


def test_another_workspace_post_is_never_matched(account, deliver):
    other = _account("other")
    _post(other, fb_post_id="111_222")
    _, send = deliver
    conversation = send("Hi", referral={**WA_AD, "source_type": "post", "source_id": "222"})
    assert AdReferral.objects.get(conversation=conversation).post is None


def test_a_chat_without_an_ad_is_unchanged(account, deliver):
    _, send = deliver
    conversation = send("How much is the blue dress?")
    assert not AdReferral.objects.exists()
    j = journey(account, reporting.last_days(30))
    # No links and no ads: nothing is trackable yet, so the journey stays empty.
    assert j is None or j["ad_chats"] is None
    assert conversation.entry_points.count() == 0


def test_an_ad_entry_point_cannot_name_a_post(account, deliver):
    from django.db import IntegrityError, transaction

    _, send = deliver
    conversation = send("Hi", referral=WA_AD)
    post = _post(account)
    entry = ConversationEntryPoint.objects.get(conversation=conversation)
    entry.post = post
    with pytest.raises(IntegrityError), transaction.atomic():
        entry.save()


# --- Instagram -------------------------------------------------------------------


def test_instagram_message_with_ad_referral_is_stored(account):
    from apps.conversations.models import Conversation

    contact = Contact.objects.create(account=account, first_name="Ig")
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.INSTAGRAM
    )
    message = Message.objects.create(
        account=account,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        body="hi",
        timestamp=timezone.now(),
    )
    data = ad_referrals.from_instagram(
        {"ad_id": "6000000001", "source": "ADS", "ads_context_data": {"ad_title": "Sale"}}
    )
    ad_referrals.record(conversation, message, AdReferral.Channel.INSTAGRAM, data)
    entry = ConversationEntryPoint.objects.get(conversation=conversation)
    assert entry.kind == "ad_referral" and entry.ad_referral.headline == "Sale"


# --- Backfill --------------------------------------------------------------------


def test_backfill_finds_referrals_in_stored_payloads(account, deliver):
    _, send = deliver
    conversation = send("Hi", referral=WA_AD)
    # Simulate a message that arrived before capture existed.
    AdReferral.objects.all().delete()
    ConversationEntryPoint.objects.all().delete()

    call_command("ad_referrals_backfill", "--dry-run")
    assert not AdReferral.objects.exists()

    call_command("ad_referrals_backfill")
    call_command("ad_referrals_backfill")  # idempotent
    assert AdReferral.objects.filter(conversation=conversation).count() == 1
