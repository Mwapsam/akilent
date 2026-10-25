"""Workstream 4: explicit, guessed and unattributed stay distinct in every report and on
lead and order pages, including for rows written before the "guesses never credit a post"
rule, and per currency. Chats and revenue are separate measures."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.commerce.models import Order
from apps.commerce.services import create_order
from apps.contacts.models import Contact
from apps.conversations import reporting
from apps.conversations.models import (
    Conversation,
    ConversationAttribution,
    ConversationEntryPoint,
    Message,
)
from apps.core import channels as ch
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.results import journey, results_for

pytestmark = pytest.mark.django_db

A = ConversationAttribution.Method


@pytest.fixture(autouse=True)
def _channels(settings):
    settings.WHATSAPP_ENABLED = True
    settings.FACEBOOK_ENABLED = True
    ch.invalidate_channel_cache()
    yield
    ch.invalidate_channel_cache()


@pytest.fixture(autouse=True)
def _no_workflows():
    with patch("apps.commerce.services._enroll_workflows"):
        yield


class World:
    def __init__(self, name):
        self.account = Account.objects.create(company_name=name)
        self.user = User.objects.create_user(name, f"{name}@x.example", "pass")
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.post = ContentPost.objects.create(
            account=self.account,
            platforms=["facebook"],
            media_type="text",
            caption="Sandals are back",
            status=ContentPost.Status.PUBLISHED,
        )
        self.variant = ContentPostVariant.objects.create(
            post=self.post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHED,
            publication_id="fb_1",
            published_at=timezone.now(),
        )
        self._n = 0

    def chat(self, *, from_post: bool):
        self._n += 1
        contact = Contact.objects.create(
            account=self.account, phone=f"+26097100{self._n:04d}"
        )
        conversation = Conversation.objects.create(
            account=self.account, contact=contact, channel=Conversation.Channel.WHATSAPP
        )
        Message.objects.create(
            account=self.account,
            conversation=conversation,
            direction=Message.Direction.INBOUND,
            body="Hi",
            timestamp=timezone.now(),
        )
        if from_post:
            ConversationEntryPoint.objects.create(
                account=self.account,
                conversation=conversation,
                kind=ConversationEntryPoint.Kind.TRACKED_LINK,
                post=self.post,
                variant=self.variant,
            )
        return conversation, contact

    def paid_order(self, contact, amount, currency, conversation=None):
        order = create_order(
            self.account,
            contact,
            [{"name": "Sandals", "unit_price": Decimal(amount), "quantity": 1}],
            currency=currency,
            conversation_id=conversation.public_id if conversation else "",
        )
        order.status = Order.Status.PAID
        order.paid_at = timezone.now()
        order.save(update_fields=["status", "paid_at"])
        return order

    def legacy_guess_order(self, contact, conversation, amount, currency):
        """An order whose guessed attribution was written before the fix, so it still
        carries the post that started the chat."""
        order = Order.objects.create(
            account=self.account,
            contact=contact,
            conversation=conversation,
            total=Decimal(amount),
            currency=currency,
            status=Order.Status.PAID,
            paid_at=timezone.now(),
        )
        entry = conversation.entry_points.first()
        ConversationAttribution.objects.create(
            account=self.account,
            conversation=conversation,
            channel=conversation.channel,
            method=A.RECENT_CONVERSATION,
            order=order,
            entry_point=entry,
            source_post=entry.post,
            source_variant=entry.variant,
        )
        return order

    def journey(self):
        return journey(self.account, reporting.last_days(30))

    def page(self, order):
        return self.client.get(
            reverse("commerce:detail", args=[order.public_id])
        ).content.decode()


@pytest.fixture
def world():
    return World("shop")


def _by_currency(rows):
    return {r["currency"]: (r["orders"], r["total"]) for r in rows}


def test_an_old_guessed_row_is_never_read_as_a_posts_sale(world):
    conversation, contact = world.chat(from_post=True)
    order = world.legacy_guess_order(contact, conversation, "60.00", "ZMW")

    assert not order.attribution.credits_post and order.attribution.is_guess
    [row] = results_for(world.post)
    assert (row["orders"], row["revenue"]) == (0, [])
    j = world.journey()
    assert (j["orders"], j["revenue"]) == (0, [])
    assert _by_currency(j["guessed_revenue"]) == {"ZMW": (1, Decimal("60.00"))}
    html = world.page(order)
    assert 'data-attribution="guess"' in html
    assert "Linked to a recent chat (a guess)" in html
    assert "From a post:" not in html


def test_the_three_revenue_buckets_are_disjoint_and_per_currency(world):
    tracked, tracked_contact = world.chat(from_post=True)
    organic, organic_contact = world.chat(from_post=False)
    _, walk_in = world.chat(from_post=False)

    world.paid_order(
        tracked_contact, "45.00", "ZMW", tracked
    )  # explicit, from the post
    world.paid_order(tracked_contact, "20.00", "USD", tracked)
    world.legacy_guess_order(tracked_contact, tracked, "60.00", "ZMW")  # a guess
    world.paid_order(organic_contact, "30.00", "USD", organic)  # named chat, no post
    Order.objects.create(  # no chat at all
        account=world.account,
        contact=walk_in,
        total=Decimal("15.00"),
        currency="ZMW",
        status=Order.Status.PAID,
        paid_at=timezone.now(),
    )

    j = world.journey()
    assert {r["currency"]: r["total"] for r in j["revenue"]} == {
        "USD": Decimal("20.00"),
        "ZMW": Decimal("45.00"),
    }
    assert _by_currency(j["guessed_revenue"]) == {"ZMW": (1, Decimal("60.00"))}
    assert _by_currency(j["untracked_revenue"]) == {
        "USD": (1, Decimal("30.00")),
        "ZMW": (1, Decimal("15.00")),
    }
    # Together they are every paid order, once, and never summed across currencies.
    totals: dict = {}
    for bucket in (j["revenue"], j["guessed_revenue"], j["untracked_revenue"]):
        for r in bucket:
            totals[r["currency"]] = totals.get(r["currency"], 0) + r["total"]
    paid = Order.objects.filter(account=world.account, status=Order.Status.PAID)
    assert totals == {
        cur: sum(o.total for o in paid if o.currency == cur) for cur in ("USD", "ZMW")
    }


def test_unattributed_chats_and_unattributed_revenue_are_separate_measures(world):
    world.chat(from_post=True)  # tracking is on
    world.chat(from_post=False)  # a chat with no order
    _, walk_in = world.chat(from_post=False)
    Conversation.objects.filter(contact=walk_in).delete()  # an order with no chat
    world.paid_order(walk_in, "15.00", "ZMW")

    j = world.journey()
    assert j["untracked_chats"] == 1
    assert _by_currency(j["untracked_revenue"]) == {"ZMW": (1, Decimal("15.00"))}


@pytest.mark.parametrize(
    "case,state,text",
    [
        ("post", "post", "From a post:"),
        ("named_chat", "none", "From a chat, not from a tracked post"),
        ("no_chat", "none", "Not from a tracked post"),
    ],
)
def test_order_pages_label_where_it_came_from(world, case, state, text):
    conversation, contact = world.chat(from_post=case == "post")
    if case == "no_chat":
        Conversation.objects.filter(pk=conversation.pk).delete()
        conversation = None
    order = world.paid_order(contact, "10.00", "ZMW", conversation)

    html = world.page(order)

    assert f'data-attribution="{state}"' in html
    assert text in html


def test_a_lead_page_uses_the_same_labels(world):
    from apps.crm.services import create_lead

    conversation, contact = world.chat(from_post=True)
    lead = create_lead(world.account, contact, conversation_id=conversation.public_id)

    html = world.client.get(
        reverse("crm:lead-detail", args=[lead.public_id])
    ).content.decode()

    assert 'data-attribution="post"' in html
    assert "made from this chat" in html
    assert "by an automation" not in html


def test_nothing_is_reported_as_untracked_before_tracking_exists(world):
    j = world.journey()
    assert j["chats"] is None
    assert (j["untracked_chats"], j["guessed_revenue"], j["untracked_revenue"]) == (
        None,
        None,
        None,
    )


def test_insights_shows_what_is_not_credited_to_a_post(world):
    conversation, contact = world.chat(from_post=True)
    world.legacy_guess_order(contact, conversation, "60.00", "ZMW")
    world.chat(from_post=False)

    html = world.client.get("/insights/").content.decode()

    assert 'data-journey="not-from-posts"' in html
    assert "ZMW 60.00 (1 order)" in html


def test_another_workspaces_attribution_never_leaks(world):
    other = World("other")
    conversation, contact = other.chat(from_post=True)
    other.paid_order(contact, "99.00", "ZMW", conversation)
    other.legacy_guess_order(contact, conversation, "50.00", "ZMW")
    other.chat(from_post=False)
    world.chat(from_post=True)  # this workspace tracks too, with one chat and no orders

    j = world.journey()
    assert (j["chats"], j["orders"], j["untracked_chats"]) == (1, 0, 0)
    assert j["revenue"] == j["guessed_revenue"] == j["untracked_revenue"] == []
    other_order = Order.objects.filter(account=other.account).first()
    assert (
        world.client.get(
            reverse("commerce:detail", args=[other_order.public_id])
        ).status_code
        == 404
    )


# --- Review findings (2026-10-25) ----------------------------------------------------------


def _credited_order(world):
    conversation, contact = world.chat(from_post=True)
    return world.paid_order(contact, "10.00", "ZMW", conversation), conversation


def test_a_credited_order_whose_entry_point_is_gone_claims_no_channel_route(world):
    order, conversation = _credited_order(world)
    ConversationEntryPoint.objects.filter(
        conversation=conversation
    ).delete()  # SET_NULL

    html = world.page(order)

    assert 'data-attribution="post"' in html
    assert "the chat started from the post" in html
    assert "comment" not in html
    assert "WhatsApp link" not in html


def test_an_automation_made_order_says_so(world):
    from apps.automation.models import Workflow, WorkflowRun

    conversation, contact = world.chat(from_post=True)
    order = Order.objects.create(
        account=world.account,
        contact=contact,
        conversation=conversation,
        total=Decimal("10.00"),
        currency="ZMW",
    )
    workflow = Workflow.objects.create(
        account=world.account, name="Auto order", definition={"steps": []}
    )
    run = WorkflowRun.objects.create(workflow=workflow, contact=contact)
    from apps.conversations import attribution

    attribution.record(order, conversation, A.EXPLICIT, workflow_run=run)

    html = world.page(order)

    assert "made from this chat by an automation" in html


def test_a_business_that_cannot_publish_sees_no_post_wording(world, settings):
    conversation, contact = world.chat(from_post=False)
    order = world.paid_order(contact, "10.00", "ZMW", conversation)
    settings.FACEBOOK_ENABLED = False
    settings.INSTAGRAM_ENABLED = False
    ch.invalidate_channel_cache()

    html = world.page(order)

    assert "data-attribution" not in html
    assert "tracked post" not in html


def test_a_credited_post_still_shows_when_publishing_is_off(world, settings):
    order, _ = _credited_order(world)
    settings.FACEBOOK_ENABLED = False
    settings.INSTAGRAM_ENABLED = False
    ch.invalidate_channel_cache()

    assert 'data-attribution="post"' in world.page(order)


def test_the_cleanup_migration_removes_post_credit_from_old_guesses(world):
    import importlib

    from django.apps import apps as django_apps

    migration = importlib.import_module(
        "apps.conversations.migrations.0025_clear_post_credit_on_guesses"
    )
    conversation, contact = world.chat(from_post=True)
    guess = world.legacy_guess_order(contact, conversation, "60.00", "ZMW")
    named = world.paid_order(contact, "45.00", "ZMW", conversation)

    migration.clear(django_apps, None)

    old = ConversationAttribution.objects.get(order=guess)
    assert old.method == A.RECENT_CONVERSATION and old.conversation == conversation
    assert (old.source_post, old.source_variant, old.entry_point) == (None, None, None)
    kept = ConversationAttribution.objects.get(order=named)
    assert kept.source_post == world.post and kept.entry_point is not None


def test_chat_counts_are_both_chats_started_in_the_period(world):
    from datetime import timedelta

    # From a post long ago, writing again now: started earlier, so in neither count.
    returning, _ = world.chat(from_post=True)
    Message.objects.filter(conversation=returning).update(
        timestamp=timezone.now() - timedelta(days=40)
    )
    ConversationEntryPoint.objects.filter(conversation=returning).update(
        matched_at=timezone.now() - timedelta(days=40)
    )
    Message.objects.create(
        account=world.account,
        conversation=returning,
        direction=Message.Direction.INBOUND,
        body="Me again",
        timestamp=timezone.now(),
    )
    # An organic chat that started long ago and got a late reply: not new either.
    old_organic, _ = world.chat(from_post=False)
    Message.objects.filter(conversation=old_organic).update(
        timestamp=timezone.now() - timedelta(days=40)
    )
    Message.objects.create(
        account=world.account,
        conversation=old_organic,
        direction=Message.Direction.INBOUND,
        body="Late",
        timestamp=timezone.now(),
    )
    world.chat(from_post=True)  # new, from a post
    world.chat(from_post=False)  # new, not from a post

    j = world.journey()

    assert (j["chats"], j["untracked_chats"]) == (1, 1)
