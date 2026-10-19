"""Post → WhatsApp chat → lead → order (Phase C).

A post's channel carries a tracked chat link. The customer's first message holds its
ref code, which links the chat to the post; leads and orders from that chat carry the
post as their source. Instagram comments on a published post link the DM a private
reply opens. Nothing is credited without that evidence.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.commerce.services import create_order, mark_paid
from apps.contacts.models import Contact
from apps.conversations.models import (
    Conversation,
    ConversationAttribution,
    ConversationEntryPoint,
    Message,
)
from apps.conversations.services import record_inbound_whatsapp_message
from apps.crm.models import Lead
from apps.insights.actions import execute_recommendation, record_outcome_signal
from apps.insights.models import Insight, RecommendationLog
from apps.publishing.links import (
    find_code,
    link_for_variant,
    short_url,
    whatsapp_url,
)
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    LinkClick,
    TrackedLink,
)
from apps.publishing.results import results_for
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppBusinessNumber, WhatsAppContact

pytestmark = pytest.mark.django_db


@pytest.fixture
def shop():
    account = Account.objects.create(company_name="Mwamba Kitchen", slug="mwamba")
    user = User.objects.create_user("owner", "owner@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id="pn1",
        display_number="+260 97 1111111",
        waba_id="waba1",
        access_token="tok",
        registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
    )
    client = Client()
    client.force_login(user)
    return account, client


def _post(account, platforms=("facebook", "instagram")):
    post = ContentPost.objects.create(
        account=account,
        platforms=list(platforms),
        media_type="image",
        caption="New stock in!",
        status=ContentPost.Status.PUBLISHED,
    )
    for p in platforms:
        ContentPostVariant.objects.create(
            post=post,
            platform=p,
            caption="New stock in!",
            status=ContentPostVariant.Status.PUBLISHED,
            publication_id=f"{p}_media_1",
        )
    return post


@pytest.fixture
def receive(shop):
    """Deliver an inbound WhatsApp message through the real inbound path."""
    account, _ = shop
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    counter = iter(range(1, 100))

    def _receive(body):
        log = MessageLog.objects.create(
            account=account,
            conversation=wa_conversation,
            contact=wa_contact,
            message_id=f"wamid.{next(counter)}",
            direction=MessageLog.Direction.INBOUND,
            message_type=MessageLog.MessageType.TEXT,
            content=body,
            status=MessageLog.Status.DELIVERED,
            timestamp=timezone.now(),
        )
        return record_inbound_whatsapp_message(
            contact=contact,
            wa_contact=wa_contact,
            whatsapp_conversation=wa_conversation,
            message_log=log,
        )

    return contact, _receive


# --- Links ----------------------------------------------------------------------


def test_each_channel_gets_its_own_short_link(shop):
    account, _ = shop
    post = _post(account)
    fb = link_for_variant(post.variants.get(platform="facebook"))
    ig = link_for_variant(post.variants.get(platform="instagram"))

    assert fb.code != ig.code
    assert fb.wa_number == "260971111111"
    assert short_url(fb).endswith(f"/go/{fb.code}")
    assert f"%28ref%20{fb.code}%29" in whatsapp_url(fb)
    # Asking again returns the same link.
    assert link_for_variant(post.variants.get(platform="facebook")) == fb


@pytest.mark.parametrize(
    "text",
    [
        "Hi! I saw your post (ref K7Q2MX)",
        "hi (Ref: k7q2mx) is it available?",
        "(ref  K7Q2MX )",
    ],
)
def test_ref_codes_survive_what_phones_do_to_them(text):
    assert find_code(text) == "K7Q2MX"


def test_no_code_no_match():
    assert find_code("Hi, I want to order") == ""


def test_go_link_counts_the_click_and_opens_whatsapp(shop):
    account, _ = shop
    link = link_for_variant(_post(account).variants.get(platform="facebook"))

    response = Client().get(f"/go/{link.code}", HTTP_USER_AGENT="Mozilla/5.0 (iPhone)")

    assert response.status_code == 302
    assert response["Location"].startswith("https://wa.me/260971111111?text=")
    click = LinkClick.objects.get(link=link)
    assert not click.is_bot


def test_link_previews_are_not_counted_as_people(shop):
    account, _ = shop
    link = link_for_variant(_post(account).variants.get(platform="facebook"))

    Client().get(f"/go/{link.code}", HTTP_USER_AGENT="facebookexternalhit/1.1")

    assert LinkClick.objects.get(link=link).is_bot


def test_a_disabled_or_unknown_link_says_so(shop):
    account, _ = shop
    link = link_for_variant(_post(account).variants.get(platform="facebook"))
    link.disabled_at = timezone.now()
    link.save()

    assert Client().get(f"/go/{link.code}").status_code == 404
    assert Client().get("/go/ZZZZZZ").status_code == 404


# --- Inbound matching -----------------------------------------------------------


def test_the_ref_code_links_the_chat_to_the_post(shop, receive):
    account, _ = shop
    post = _post(account)
    link = link_for_variant(post.variants.get(platform="facebook"))
    _, deliver = receive

    conversation = deliver(f"Hi! I saw your post (ref {link.code})")

    entry = ConversationEntryPoint.objects.get(conversation=conversation)
    assert entry.post == post
    assert entry.variant.platform == "facebook"
    assert entry.kind == ConversationEntryPoint.Kind.TRACKED_LINK


def test_another_businesss_code_is_ignored(shop, receive):
    account, _ = shop
    other = Account.objects.create(company_name="Other", slug="other")
    WhatsAppBusinessNumber.objects.create(
        account=other,
        phone_number_id="pn2",
        display_number="+260 97 2222222",
        waba_id="w2",
        access_token="t",
        registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
    )
    foreign = link_for_variant(_post(other).variants.get(platform="facebook"))
    _, deliver = receive

    deliver(f"Hi (ref {foreign.code})")

    assert not ConversationEntryPoint.objects.exists()


def test_lead_and_order_from_the_chat_carry_the_post(shop, receive):
    account, _ = shop
    post = _post(account)
    variant = post.variants.get(platform="facebook")
    link = link_for_variant(variant)
    contact, deliver = receive

    # A buying question opens a lead automatically.
    conversation = deliver(f"How much is it? (ref {link.code})")
    lead = Lead.objects.get(account=account, contact=contact)
    assert lead.attribution.source_post == post
    assert lead.attribution.source_variant == variant
    assert lead.attribution.entry_point.link == link

    order = create_order(
        account,
        contact,
        [{"name": "Dress", "unit_price": Decimal("120.00"), "quantity": 1}],
        currency="ZMW",
        conversation_id=conversation.public_id,
    )
    assert order.attribution.source_variant == variant
    payment = order.payments.create(
        account=account, amount=order.total, currency=order.currency
    )
    mark_paid(payment, transaction_id="tx1")

    LinkClick.objects.create(link=link)
    [row] = [r for r in results_for(post) if r["variant"] == variant]
    assert (row["clicks"], row["chats"], row["leads"], row["orders"]) == (1, 1, 1, 1)
    assert row["revenue"] == [{"currency": "ZMW", "total": Decimal("120.00")}]


def test_a_chat_without_evidence_stays_unattributed(shop, receive):
    account, _ = shop
    _post(account)
    contact, deliver = receive

    deliver("How much is the blue dress?")

    lead = Lead.objects.get(account=account, contact=contact)
    assert lead.attribution.source_post is None
    assert lead.attribution.entry_point is None


# --- Instagram comments ---------------------------------------------------------


def test_a_comment_dm_on_a_published_post_is_linked(shop):
    from apps.instagram.services.inbound import _process_comment_entry
    from apps.instagram.services.triggers import _ensure_conversation
    from apps.instagram.tests.helpers import make_instagram_account

    account, _ = shop
    post = _post(account, platforms=("instagram",))
    variant = post.variants.get()
    ig_account = make_instagram_account(account)

    _process_comment_entry(
        ig_account,
        {
            "id": "c1",
            "text": "price?",
            "from": {"id": "igsid_1", "username": "chanda"},
            "media": {"id": variant.publication_id},
        },
    )
    from apps.instagram.models import CommentThread

    thread = CommentThread.objects.get(comment_id="c1")
    assert thread.content_variant == variant

    _ensure_conversation(thread, thread.instagram_contact)

    entry = ConversationEntryPoint.objects.get(conversation_id=thread.conversation_id)
    assert entry.kind == ConversationEntryPoint.Kind.IG_COMMENT
    assert entry.variant == variant


# --- Phase 6: insight → DM → lead → order ---------------------------------------


def _insight_dm(account, contact):
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel="instagram"
    )
    insight = Insight.objects.create(
        account=account, type="unanswered_intent", title="Follow up"
    )
    rec = RecommendationLog.objects.create(account=account, insight=insight)
    execute_recommendation(rec, conversation=conversation, action_type="send_dm")
    record_outcome_signal(rec, "dm_sent_at")
    return conversation, rec


def test_insight_chain_is_linked_without_hand_made_rows(shop):
    account, _ = shop
    contact = Contact.objects.create(account=account, phone="+260970000001")
    conversation, rec = _insight_dm(account, contact)

    Message.objects.create(
        account=account,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        body="yes please",
        timestamp=timezone.now(),
    )
    order = create_order(
        account,
        contact,
        [{"name": "Thing", "unit_price": Decimal("10.00"), "quantity": 1}],
        conversation_id=conversation.public_id,
    )

    assert order.attribution.originated_from == rec
    rec.refresh_from_db()
    assert rec.outcome_summary.get("order_created_at")


def test_a_reply_to_an_insight_dm_is_recorded(shop):
    from apps.conversations.entry_points import on_inbound_message

    account, _ = shop
    contact = Contact.objects.create(account=account, phone="+260970000002")
    conversation, rec = _insight_dm(account, contact)
    message = Message.objects.create(
        account=account,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        body="hello",
        timestamp=timezone.now(),
    )

    on_inbound_message(conversation, message)

    rec.refresh_from_db()
    assert rec.outcome_summary.get("dm_replied_at")


# --- Composer and post page -----------------------------------------------------


def test_composer_adds_the_link_to_the_facebook_caption(shop):
    account, client = shop

    client.post(
        reverse("publishing:post-new"),
        {
            "caption": "Weekend sale",
            "platforms": ["facebook", "instagram"],
            "media_type": "image",
            "wa_link_facebook": "1",
            "wa_link_instagram": "1",
            "action": "draft",
        },
    )

    post = ContentPost.objects.get(account=account)
    fb = post.variants.get(platform="facebook")
    ig = post.variants.get(platform="instagram")
    assert f"/go/{fb.tracked_link.code}" in fb.caption
    assert "/go/" not in ig.caption  # Instagram captions can't hold tappable links
    assert ig.tracked_link.disabled_at is None


def test_unticking_turns_the_link_off_and_removes_it_from_the_caption(shop):
    account, client = shop
    data = {
        "caption": "Weekend sale",
        "platforms": ["facebook"],
        "media_type": "image",
        "wa_link_facebook": "1",
        "action": "draft",
    }
    client.post(reverse("publishing:post-new"), data)
    post = ContentPost.objects.get(account=account)

    data.pop("wa_link_facebook")
    client.post(reverse("publishing:post-new"), {**data, "post_id": post.pk})

    fb = post.variants.get(platform="facebook")
    assert "/go/" not in fb.caption
    assert TrackedLink.objects.get(variant=fb).disabled_at is not None


def test_composer_without_a_whatsapp_number_offers_to_connect(shop):
    account, client = shop
    WhatsAppBusinessNumber.objects.filter(account=account).delete()

    html = client.get(reverse("publishing:post-new")).content.decode()

    assert "Connect a WhatsApp number" in html


def test_post_page_shows_results_and_the_link(shop):
    account, client = shop
    post = _post(account, platforms=("instagram",))
    link = link_for_variant(post.variants.get())

    html = client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()

    assert "Results" in html
    assert f"/go/{link.code}" in html
    assert "bio or on a story link sticker" in html


def test_order_page_says_which_post_it_came_from(shop, receive):
    account, client = shop
    post = _post(account)
    link = link_for_variant(post.variants.get(platform="facebook"))
    contact, deliver = receive
    conversation = deliver(f"Hi (ref {link.code})")
    with patch("apps.commerce.services._enroll_workflows"):
        order = create_order(
            account,
            contact,
            [{"name": "Thing", "unit_price": Decimal("10.00"), "quantity": 1}],
            conversation_id=conversation.public_id,
        )

    html = client.get(
        reverse("commerce:detail", args=[order.public_id])
    ).content.decode()

    assert "Came from a post" in html
    assert "used the post's WhatsApp link" in html


def test_attribution_with_a_post_is_still_immutable(shop, receive):
    account, _ = shop
    post = _post(account)
    link = link_for_variant(post.variants.get(platform="facebook"))
    contact, deliver = receive
    deliver(f"How much? (ref {link.code})")
    attribution = ConversationAttribution.objects.get(lead__contact=contact)

    with pytest.raises(ValueError, match="historical record"):
        attribution.save()
