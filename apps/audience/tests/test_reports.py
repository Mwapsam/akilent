"""Stage 2 (topics in Comments, the post's "What people are saying") and Stage 4
(comment-level outcomes and coverage). docs/plans/audience-intelligence-spec.md §7, §9."""

from __future__ import annotations

import secrets
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.audience import services
from apps.audience.models import AudienceSignalCorrection
from apps.audience.reports import post_audience, topics_for
from apps.billing.models import AccountFeatureOverride
from apps.commerce.models import Order
from apps.conversations.models import Conversation
from apps.instagram.models.comment import Comment, CommentThread
from apps.instagram.tests.helpers import (
    make_account,
    make_instagram_account,
    make_instagram_contact,
)
from apps.publishing.models import ContentPost, ContentPostVariant

pytestmark = pytest.mark.django_db

RELEASED = frozenset({"chatbot", "support", "hubspot", "audience_insights"})


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.commerce.services._enroll_workflows"),
        patch("apps.crm.services._enroll_workflows"),
    ):
        yield


class Shop:
    def __init__(self):
        self.account, self.user = make_account()
        AccountFeatureOverride.objects.create(
            account=self.account, key="instagram", grant=True
        )
        self.iba = make_instagram_account(self.account)
        self.post = self.new_post("m_1")
        self.variant = self.post.variants.get()

    def new_post(self, media_id):
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="text",
            caption="New sandals",
            status=ContentPost.Status.PUBLISHED,
            published_at=timezone.now(),
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PUBLISHED,
            publication_id=media_id,
            published_at=timezone.now(),
        )
        return post

    def person(self, username):
        ig_contact, _ = make_instagram_contact(self.account, self.iba)
        ig_contact.username = username
        ig_contact.save(update_fields=["username"])
        return ig_contact

    def say(self, who, body, *, state="pending", hidden=False, variant=None):
        variant = variant or self.variant
        cid = f"c_{secrets.token_hex(5)}"
        thread = CommentThread.objects.create(
            instagram_account=self.iba,
            post_id=variant.publication_id,
            instagram_contact=who,
            comment_id=cid,
            body=body,
            received_at=timezone.now(),
            content_variant=variant,
        )
        comment = Comment.objects.create(
            thread=thread,
            comment_id=cid,
            instagram_contact=who,
            body=body,
            timestamp=timezone.now(),
            moderation_state=state,
            is_hidden=hidden,
        )
        services.classify_comment(comment)
        return comment

    def answer_publicly(self, comment):
        Comment.objects.create(
            thread=comment.thread,
            comment_id=f"r_{secrets.token_hex(5)}",
            instagram_contact=comment.instagram_contact,
            body="Thanks!",
            direction=Comment.Direction.OUTBOUND,
            timestamp=timezone.now(),
        )

    def private_reply_opened(self, comment, *, active=True):
        """The private reply to ``comment`` opened a DM (as the trigger/view does)."""
        from apps.conversations.entry_points import record_comment_entry

        conversation = Conversation.objects.create(
            account=self.account,
            contact=comment.instagram_contact.contact,
            channel=Conversation.Channel.INSTAGRAM,
            last_message_at=timezone.now()
            if active
            else timezone.now() - timezone.timedelta(days=40),
        )
        thread = comment.thread
        thread.conversation = conversation
        thread.save(update_fields=["conversation"])
        record_comment_entry(thread)
        return conversation

    def paid_order(self, conversation, amount="40.00"):
        from apps.commerce.services import create_order

        order = create_order(
            self.account,
            conversation.contact,
            [{"name": "Sandals", "unit_price": Decimal(amount), "quantity": 1}],
            currency="USD",
            conversation_id=conversation.public_id,
        )
        order.status = Order.Status.PAID
        order.paid_at = timezone.now()
        order.save(update_fields=["status", "paid_at"])
        return order

    def lead(self, conversation):
        from apps.crm.services import create_lead

        return create_lead(
            self.account, conversation.contact, conversation_id=conversation.public_id
        )


@pytest.fixture
def shop():
    return Shop()


def _row(audience, category):
    return next(r for r in audience.rows if r.category == category)


# --- the post summary (Stage 2) ------------------------------------------------------


def test_counts_are_people_not_comments(shop):
    amy = shop.person("amy")
    shop.say(amy, "How much?")
    shop.say(amy, "how much?? price please")
    shop.say(shop.person("ben"), "Do you have it in size 6?")

    audience = post_audience(shop.post)
    buying = _row(audience, "buying_intent")
    assert (buying.commenters, buying.comments) == (2, 3)
    assert audience.commenters == 2 and audience.comments == 3
    question = _row(audience, "product_enquiry")
    assert question.commenters == 1 and ("size", 1) in question.subs


def test_deleted_comments_are_left_out_and_hidden_ones_never_shown(shop):
    shop.say(shop.person("amy"), "love it", state="deleted")
    shop.say(shop.person("ben"), "gorgeous", state="hidden", hidden=True)
    shop.say(shop.person("cat"), "so pretty")

    praise = _row(post_audience(shop.post), "praise")
    assert praise.commenters == 2
    assert [e.body for e in praise.examples] == ["so pretty"]


def test_unanswered_counts_people_nobody_answered(shop):
    shop.say(shop.person("amy"), "how much?")
    answered = shop.say(shop.person("ben"), "price?")
    shop.answer_publicly(answered)
    dm = shop.say(shop.person("cat"), "how much is it")
    shop.private_reply_opened(dm)

    assert _row(post_audience(shop.post), "buying_intent").unanswered == 1


def test_chat_only_is_not_a_row_and_nobody_commenting_is_none(shop):
    assert post_audience(shop.post) is None
    shop.say(shop.person("amy"), "@friend")
    audience = post_audience(shop.post)
    assert audience.rows == [] and audience.commenters == 1 and audience.specific == 0


def test_owner_corrections_change_the_summary(shop):
    c = shop.say(shop.person("amy"), "love it")
    services.set_topics(c, ["complaint"])
    audience = post_audience(shop.post)
    assert [r.category for r in audience.rows] == ["complaint"]


# --- outcomes and coverage (Stage 4) -------------------------------------------------


def test_comment_level_outcomes_and_coverage(shop):
    amy = shop.say(shop.person("amy"), "How much?")
    ben = shop.say(shop.person("ben"), "price please")
    shop.say(shop.person("cat"), "how much")
    quiet = shop.say(shop.person("dan"), "price?")

    order_conv = shop.private_reply_opened(amy)
    shop.lead(order_conv)
    shop.paid_order(order_conv, "40.00")
    shop.private_reply_opened(ben)  # still talking
    shop.private_reply_opened(quiet, active=False)  # went quiet

    audience = post_audience(shop.post)
    o = _row(audience, "buying_intent").outcome
    assert (o.linked, o.unlinked) == (3, 1)
    assert (o.leads, o.orders) == (1, 1)
    assert o.revenue == [{"currency": "USD", "total": Decimal("40.00")}]
    assert (o.in_progress, o.no_outcome) == (1, 1)
    assert (audience.linked, audience.unlinked) == (3, 1)


def test_an_order_credited_to_another_post_is_not_this_comments_outcome(shop):
    from apps.conversations.models import ConversationEntryPoint

    amy = shop.say(shop.person("amy"), "How much?")
    conv = shop.private_reply_opened(amy)
    # The chat's first explicit touch was another post's link, so the order credits it.
    other = shop.new_post("m_2")
    ConversationEntryPoint.objects.filter(conversation=conv).delete()
    ConversationEntryPoint.objects.create(
        account=shop.account,
        conversation=conv,
        kind=ConversationEntryPoint.Kind.TRACKED_LINK,
        post=other,
        variant=other.variants.get(),
    )
    order = shop.paid_order(conv)
    assert order.attribution.source_post == other

    o = _row(post_audience(shop.post), "buying_intent").outcome
    assert o.linked == 1 and o.orders == 0 and o.revenue == []


def test_a_reply_never_claims_the_threads_conversation(shop):
    amy = shop.say(shop.person("amy"), "love it")
    shop.private_reply_opened(amy)
    # Ben replies under Amy's comment: Amy's DM isn't Ben's outcome.
    ben = shop.person("ben")
    Comment.objects.create(
        thread=amy.thread,
        comment_id=f"r_{secrets.token_hex(4)}",
        parent_comment_id=amy.comment_id,
        instagram_contact=ben,
        body="how much?",
        timestamp=timezone.now(),
    )
    services.classify_comment(Comment.objects.get(instagram_contact=ben))
    o = _row(post_audience(shop.post), "buying_intent").outcome
    assert (o.linked, o.unlinked) == (0, 1)


# --- topics on the Comments list ----------------------------------------------------


def test_topics_drop_chat_and_list_primary_first(shop):
    c = shop.say(shop.person("amy"), "Love it! how much?")
    chat = shop.say(shop.person("ben"), "hello")
    found = topics_for([("instagram", c.pk, c.body), ("instagram", chat.pk, chat.body)])
    assert [t.category for t in found[("instagram", c.pk)]] == [
        "buying_intent",
        "praise",
    ]
    assert found[("instagram", chat.pk)] == []


def _client(shop):
    client = Client()
    client.force_login(shop.user)
    return client


@override_settings(RELEASED_FEATURES=RELEASED)
def test_comments_page_shows_topics_and_filters_by_them(shop):
    shop.say(shop.person("amy"), "how much is this?")
    shop.say(shop.person("ben"), "never arrived, still waiting")
    client = _client(shop)

    html = client.get(
        reverse("conversations:comments") + "?filter=all"
    ).content.decode()
    assert "data-topic-filter" in html
    assert 'data-topic="buying_intent"' in html and 'data-topic="complaint"' in html

    html = client.get(
        reverse("conversations:comments") + "?filter=all&topic=complaint"
    ).content.decode()
    assert "never arrived" in html and "how much is this" not in html


def test_unreleased_comments_page_shows_no_topics(shop):
    shop.say(shop.person("amy"), "how much is this?")
    html = _client(shop).get(
        reverse("conversations:comments") + "?filter=all&topic=complaint"
    )
    html = html.content.decode()
    assert "data-topic" not in html
    assert "how much is this" in html  # the topic is ignored, not applied


@override_settings(RELEASED_FEATURES=RELEASED)
def test_the_owner_corrects_topics_from_the_comments_page(shop):
    c = shop.say(shop.person("amy"), "love it")
    url = reverse("conversations:comments_topics", args=["instagram", c.pk])
    _client(shop).post(url, {"topics": ["complaint", "nonsense"]})
    assert {s.category for s in services.current_signals(c)} == {"complaint"}

    # Ticking praise back drops the reject rather than stacking an add on it.
    _client(shop).post(url, {"topics": ["praise"]})
    assert {s.category for s in services.current_signals(c)} == {"praise"}
    assert not AudienceSignalCorrection.objects.filter(action="reject").exists()
    assert not AudienceSignalCorrection.objects.filter(category="complaint").exists()


def test_topic_corrections_need_the_release(shop):
    c = shop.say(shop.person("amy"), "love it")
    url = reverse("conversations:comments_topics", args=["instagram", c.pk])
    _client(shop).post(url, {"topics": ["complaint"]})
    assert not AudienceSignalCorrection.objects.exists()


def test_topic_corrections_stay_inside_the_account(shop):
    c = shop.say(shop.person("amy"), "love it")
    other = Shop()
    url = reverse("conversations:comments_topics", args=["instagram", c.pk])
    with override_settings(RELEASED_FEATURES=RELEASED):
        _client(other).post(url, {"topics": ["complaint"]})
    assert not AudienceSignalCorrection.objects.exists()


# --- the post page --------------------------------------------------------------------


def test_post_page_shows_what_people_are_saying_once_released(shop):
    shop.say(shop.person("amy"), "Do you deliver to Leeds?")
    url = reverse("publishing:post-detail", args=[shop.post.pk])
    AccountFeatureOverride.objects.create(
        account=shop.account, key="content_studio", grant=True
    )
    client = _client(shop)
    assert "data-post-audience" not in client.get(url).content.decode()
    with override_settings(RELEASED_FEATURES=RELEASED):
        html = client.get(url).content.decode()
    assert "What people are saying" in html
    assert 'data-audience-row="product_enquiry"' in html and "data-coverage" in html


# --- diagnostics command --------------------------------------------------------------


def test_audience_report_prints_coverage_and_topics(shop):
    amy = shop.say(shop.person("amy"), "how much?")
    shop.say(shop.person("ben"), "random words")
    shop.paid_order(shop.private_reply_opened(amy))
    out = StringIO()
    call_command("audience_report", "--account", str(shop.account.pk), stdout=out)
    text = out.getvalue()
    assert "Comments: 2 from 2 people" in text
    assert "general only): 1 (50%)" in text
    assert "Wants to buy: 1 · 1 · 0 · 1" in text
