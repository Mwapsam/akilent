"""Workstream 5: one workspace can never reach, change, count or confirm another's
publishing and sales records: not through ids, ref codes, filters, reports or error
messages."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.commerce.models import Order, Payment
from apps.commerce.services import create_order
from apps.contacts.models import Contact
from apps.conversations import reporting
from apps.conversations.entry_points import match_ref_code
from apps.conversations.models import Conversation, ConversationEntryPoint, Message
from apps.core import channels as ch
from apps.crm.models import Lead
from apps.publishing.links import link_for_variant
from apps.publishing.models import (
    ApprovalLog,
    ContentPost,
    ContentPostVariant,
    PublishingSettings,
)
from apps.publishing.results import journey, results_for
from apps.publishing.worklist import worklist
from apps.whatsapp.models import WhatsAppBusinessNumber

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status


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


def _workspace(name, phone_id):
    account = Account.objects.create(company_name=name)
    user = User.objects.create_user(name, f"{name}@x.example", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id=phone_id,
        display_number="+260 97 1111111",
        waba_id=f"waba-{name}",
        access_token="t",
        registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
    )
    client = Client()
    client.force_login(user)
    return account, user, client


@pytest.fixture
def mine():
    return _workspace("mine", "PN-MINE")


@pytest.fixture
def theirs():
    """Another business with a published post, a reconciling channel, a chat from the
    post, a lead and a paid order."""
    account, user, _ = _workspace("theirs", "PN-THEIRS")
    PublishingSettings.objects.create(account=account, requires_review=True)
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook", "instagram"],
        media_type="text",
        caption="Their secret launch",
        status=ContentPost.Status.PUBLISHING,
        created_by=user,
        approval_status="submitted",
    )
    fb = ContentPostVariant.objects.create(
        post=post,
        platform="facebook",
        status=S.FAILED,
        last_error="Their error detail",
        published_at=timezone.now(),
    )
    ig = ContentPostVariant.objects.create(
        post=post, platform="instagram", status=S.RECONCILING
    )
    link = link_for_variant(fb)
    contact = Contact.objects.create(account=account, phone="+260979999999")
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP
    )
    message = Message.objects.create(
        account=account,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        body=f"Hi (ref {link.code})",
        timestamp=timezone.now(),
    )
    match_ref_code(conversation, message)
    order = create_order(
        account,
        contact,
        [{"name": "Secret", "unit_price": Decimal("99.00"), "quantity": 1}],
        currency="ZMW",
        conversation_id=conversation.public_id,
    )
    order.status = Order.Status.PAID
    order.paid_at = timezone.now()
    order.save(update_fields=["status", "paid_at"])
    return {
        "account": account,
        "post": post,
        "fb": fb,
        "ig": ig,
        "link": link,
        "conversation": conversation,
        "order": order,
    }


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


# --- Publishing: ids ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["reconciliation-confirm", "reconciliation-retry", "reconciliation-unresolved"],
)
def test_reconciliation_treats_another_workspaces_channel_as_missing(
    mine, theirs, name
):
    _, user, _ = mine
    data = {"publication_id": "x", "evidence_notes": "n", "reason": "r"}
    # A fresh session each, so each response carries only its own message.
    first, second = Client(), Client()
    first.force_login(user)
    second.force_login(user)

    theirs_res = first.post(reverse(f"publishing:{name}", args=[theirs["ig"].pk]), data)
    missing_res = second.post(reverse(f"publishing:{name}", args=[999999]), data)

    assert _messages(theirs_res) == _messages(missing_res) == ["Variant not found."]
    assert theirs_res["Location"] == missing_res["Location"]
    theirs["ig"].refresh_from_db()
    assert theirs["ig"].status == S.RECONCILING
    assert not theirs["ig"].publication_id


def test_the_reconciliation_queue_lists_only_your_channels(mine, theirs):
    _, _, client = mine

    html = client.get(reverse("publishing:reconciliation-queue")).content.decode()

    assert "Their secret launch" not in html


@pytest.mark.parametrize(
    "method,name,args",
    [
        ("get", "post-detail", lambda t: [t["post"].pk]),
        ("post", "post-retry", lambda t: [t["post"].pk]),
        ("post", "post-retry-variant", lambda t: [t["post"].pk, "facebook"]),
        ("post", "post-approve", lambda t: [t["post"].pk]),
        ("post", "post-request-changes", lambda t: [t["post"].pk]),
        ("post", "post-submit-for-review", lambda t: [t["post"].pk]),
        ("post", "post-cancel", lambda t: [t["post"].pk]),
        ("post", "post-delete", lambda t: [t["post"].pk]),
        ("post", "first-comment-retry", lambda t: [t["post"].pk, t["fb"].pk]),
    ],
)
def test_another_workspaces_post_cannot_be_opened_or_changed(
    mine, theirs, method, name, args
):
    _, _, client = mine
    url = reverse(f"publishing:{name}", args=args(theirs))

    with patch("apps.publishing.tasks.publish_variant_async.delay") as queued:
        res = getattr(client, method)(url, {"feedback": "x"})

    assert res.status_code == 404
    queued.assert_not_called()
    post = ContentPost.objects.get(pk=theirs["post"].pk)
    assert (post.status, post.approval_status) == (
        ContentPost.Status.PUBLISHING,
        "submitted",
    )
    assert not ApprovalLog.objects.filter(post=post).exists()
    assert ContentPostVariant.objects.get(pk=theirs["fb"].pk).status == S.FAILED


def test_the_composer_ignores_another_workspaces_post_id(mine, theirs):
    _, _, client = mine

    html = client.get(
        reverse("publishing:post-new"), {"post_id": theirs["post"].pk}
    ).content.decode()

    assert "Their secret launch" not in html


# --- Publishing: lists, counts and reports -----------------------------------------------


def test_lists_counts_and_reports_hold_only_your_records(mine, theirs):
    account, user, client = mine

    work = worklist(account, user, lambda _p: True)
    assert sum(work["counts"].values()) == 0
    for bucket in ("failed", "needs_check", "awaiting_approval"):
        res = client.get(reverse("publishing:library"), {"bucket": bucket})
        assert list(res.context["page_obj"].object_list) == []
    html = client.get(reverse("publishing:index")).content.decode()
    assert "Their secret launch" not in html and "Their error detail" not in html
    assert journey(account, reporting.last_days(30))["chats"] is None
    assert reporting.revenue(account, reporting.last_days(30)) == []


def test_a_posts_results_count_only_its_own_workspaces_evidence(mine, theirs):
    """Their card stays exactly theirs even after my customer sends their ref code."""
    account, _, _ = mine
    contact = Contact.objects.create(account=account, phone="+260971000001")
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP
    )
    message = Message.objects.create(
        account=account,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        body=f"Saw this (ref {theirs['link'].code})",
        timestamp=timezone.now(),
    )

    assert match_ref_code(conversation, message) is None
    assert not ConversationEntryPoint.objects.filter(account=account).exists()
    [row] = [r for r in results_for(theirs["post"]) if r["variant"] == theirs["fb"]]
    assert (row["chats"], row["orders"]) == (1, 1)
    assert {r["currency"]: r["total"] for r in row["revenue"]} == {
        "ZMW": Decimal("99.00")
    }


def test_another_workspaces_link_still_works_for_its_customers(mine, theirs):
    """The public redirect is not workspace-gated: it is the business's own customers'
    entry point, and the code reveals nothing beyond the prefilled message."""
    res = Client().get(f"/go/{theirs['link'].code}")

    assert res.status_code == 302 and res["Location"].startswith("https://wa.me/")


# --- Sales --------------------------------------------------------------------------------


def test_another_workspaces_order_cannot_be_opened_or_marked_paid(mine, theirs):
    _, _, client = mine
    order = theirs["order"]
    url = reverse("commerce:detail", args=[order.public_id])
    before = (order.status, order.paid_at, order.paid_method, order.updated_at)

    assert client.get(url).status_code == 404
    assert (
        client.post(url, {"action": "mark_paid", "method": "cash"}).status_code == 404
    )
    assert client.post(url, {"action": "mark_unpaid"}).status_code == 404

    order.refresh_from_db()
    assert (order.status, order.paid_at, order.paid_method, order.updated_at) == before
    assert Payment.objects.filter(order=order).count() == 0


@pytest.mark.parametrize("view", ["commerce:create-order", "crm:create-lead"])
def test_another_workspaces_chat_cannot_be_credited(mine, theirs, view):
    account, _, client = mine
    Contact.objects.create(account=account, phone="+260979999999")  # same number, mine

    res = client.post(
        reverse(view),
        {
            "contact": "+260979999999",
            "conversation": theirs["conversation"].public_id,
            "name": "x",
            "unit_price": "1",
        },
    )

    assert res.status_code == 302
    assert "That conversation could not be found." in _messages(res)
    assert not Order.objects.filter(account=account).exists()
    assert not Lead.objects.filter(account=account).exists()
    assert (
        not Order.objects.filter(conversation=theirs["conversation"])
        .exclude(pk=theirs["order"].pk)
        .exists()
    )
