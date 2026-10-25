"""Release gate (Workstream 3): one post, through the real application paths, from the
composer to a paid order in the reports, plus the negative controls.

Only external HTTP is mocked: Meta's Graph API for publishing (``FacebookPublishProvider``
``_post``/``_get``) and Flutterwave's client. Everything else runs for real:
- the composer and review views;
- the Celery publish task (run eagerly in tests) and the ``/go/<code>`` redirect;
- the signed WhatsApp webhook through ``process_whatsapp_event``;
- the lead and order views, the payment request and the signed Flutterwave webhook.

Each step checks the saved rows and the reports, so a failure names the link that broke.
Passing this gate is code correctness only; the real staging journey is a separate
requirement (docs/STAGING-VALIDATION-CHECKLIST.md).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from decimal import Decimal
from itertools import count
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.commerce.models import Order
from apps.contacts.models import Contact
from apps.conversations import reporting
from apps.conversations.models import (
    Conversation,
    ConversationAttribution,
    ConversationEntryPoint,
    Message,
)
from apps.core import channels as ch
from apps.crm.models import Lead
from apps.publishing.models import (
    ApprovalLog,
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
    LinkClick,
    PublishingSettings,
    TrackedLink,
)
from apps.publishing.providers.facebook import FacebookPublishProvider
from apps.publishing.results import journey, results_for
from apps.publishing.worklist import bucket_for
from apps.whatsapp.models import WhatsAppBusinessNumber

pytestmark = pytest.mark.django_db

A = ConversationAttribution.Method
CUSTOMER = "260971234567"
ORGANIC_CUSTOMER = "260977654321"
BROWSER = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"


@pytest.fixture(autouse=True)
def _channels(settings):
    settings.WHATSAPP_ENABLED = True
    settings.FACEBOOK_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    ch.invalidate_channel_cache()
    yield
    ch.invalidate_channel_cache()


@pytest.fixture(autouse=True)
def _no_outbound_side_effects():
    """Inbound processing runs for real; replies, read receipts and automations don't."""
    targets = (
        "apps.whatsapp.tasks.mark_read",
        "apps.whatsapp.tasks.dispatcher",
        "apps.whatsapp.tasks.drain_outbound_queue",
        "apps.whatsapp.tasks._auto_reply_during_setup",
    )
    patchers = [patch(t) for t in targets]
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()


class Graph:
    """Meta's Graph API for Page publishing: records what was sent, answers like Meta."""

    def __init__(self):
        self.calls = []

    def post(self, endpoint, **payload):
        self.calls.append((endpoint, payload))
        return {"id": "PAGE_POST_1", "post_id": "PAGE_1_POST_1"}

    def get(self, path, **params):
        return {}


class Shop:
    """The business, its team, and helpers that drive each step through the real views."""

    def __init__(self, settings, capture):
        # Requests run their commit hooks (the queued Celery tasks) as they would live.
        self.capture = capture
        self.account = Account.objects.create(
            company_name="Journey Shop", slug="journey"
        )
        self.writer = self._member("writer", Membership.Role.ADMIN)
        self.reviewer = self._member("reviewer", Membership.Role.OWNER)
        self.writer_client = self._client(self.writer)
        self.reviewer_client = self._client(self.reviewer)
        PublishingSettings.objects.create(account=self.account, requires_review=True)
        FacebookPageConnection.objects.create(
            account=self.account,
            page_id="PAGE_1",
            page_name="Journey Shop",
            access_token="page-token",
        )
        WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="PNID",
            display_number="+260 97 1111111",
            waba_id="WABA",
            access_token="wa-token",
            registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
        )
        self.graph = Graph()
        self._ids = count(1)
        settings.WHATSAPP_APP_SECRET = "test_app_secret"

    def _member(self, name, role):
        user = User.objects.create_user(name, f"{name}@journey.example", "pass")
        Membership.objects.create(user=user, account=self.account, role=role)
        return user

    @staticmethod
    def _client(user):
        client = Client()
        client.force_login(user)
        return client

    # -- content ---------------------------------------------------------------------------
    def compose(self, client, **extra):
        data = {
            "caption": "New summer sandals are in!",
            "platforms": ["facebook"],
            "media_type": "text",
            "cta_kind": "whatsapp",
            "wa_link_facebook": "1",
            "action": "submit_review",
        }
        data.update(extra)
        with self.capture(execute=True):
            return client.post(reverse("publishing:post-new"), data)

    # -- customers ---------------------------------------------------------------------------
    def whatsapp_message(self, phone, text, msg_id=None, *, body=None):
        """POST a signed Meta webhook to the real endpoint. Returns (response, raw body)."""
        if body is None:
            body = json.dumps(
                {
                    "object": "whatsapp_business_account",
                    "entry": [
                        {
                            "id": "WABA",
                            "changes": [
                                {
                                    "field": "messages",
                                    "value": {
                                        "messaging_product": "whatsapp",
                                        "metadata": {
                                            "display_phone_number": "260971111111",
                                            "phone_number_id": "PNID",
                                        },
                                        "contacts": [
                                            {
                                                "profile": {"name": "Mary"},
                                                "wa_id": phone,
                                            }
                                        ],
                                        "messages": [
                                            {
                                                "from": phone,
                                                "id": msg_id
                                                or f"wamid.{next(self._ids)}",
                                                "timestamp": str(int(time.time())),
                                                "type": "text",
                                                "text": {"body": text},
                                            }
                                        ],
                                    },
                                }
                            ],
                        }
                    ],
                }
            ).encode()
        signature = hmac.new(b"test_app_secret", body, hashlib.sha256).hexdigest()
        with self.capture(execute=True):
            response = Client().post(
                reverse("whatsapp-webhook"),
                data=body,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=f"sha256={signature}",
            )
        return response, body

    def conversation_of(self, phone):
        contact = Contact.objects.get(account=self.account, phone=f"+{phone}")
        return Conversation.objects.get(account=self.account, contact=contact), contact

    # -- sales ---------------------------------------------------------------------------
    def create_lead(self, phone, conversation=None):
        with self.capture(execute=True):
            return self.reviewer_client.post(
                reverse("crm:create-lead"),
                {
                    "contact": f"+{phone}",
                    "conversation": conversation.public_id if conversation else "",
                },
            )

    def create_order(self, phone, price, currency, conversation=None):
        before = set(Order.objects.values_list("pk", flat=True))
        with self.capture(execute=True):
            self._post_order(phone, price, currency, conversation)
        return Order.objects.exclude(pk__in=before).get()

    def _post_order(self, phone, price, currency, conversation):
        self.reviewer_client.post(
            reverse("commerce:create-order"),
            {
                "contact": f"+{phone}",
                "conversation": conversation.public_id if conversation else "",
                "name": "Sandals",
                "unit_price": str(price),
                "quantity": "1",
                "currency": currency,
            },
        )

    def pay(self, order, tx_id, *, replay=False):
        """Request a payment link, then deliver Flutterwave's signed charge webhook.

        ``replay`` only redelivers the webhook, as Flutterwave does when it retries.
        """
        if not replay:
            self.request_payment(order)
        return self.charge_webhook(order, tx_id)

    def request_payment(self, order):
        with (
            patch("apps.billing.flutterwave.get_fw_client") as fw,
            self.capture(execute=True),
        ):
            fw.return_value.initialize_payment.return_value = (
                "https://checkout.example/1"
            )
            self.reviewer_client.post(
                reverse("commerce:detail", args=[order.public_id]),
                {"action": "request_payment"},
            )

    def charge_webhook(self, order, tx_id):
        payment = order.payments.order_by("-created_at").first()
        payload = {
            "event": "charge.completed",
            "data": {
                "id": tx_id,
                "status": "successful",
                "amount": str(order.total),
                "currency": order.currency,
                "customer": {"email": "buyer@example.com"},
                "meta": {"order_id": order.public_id, "payment_id": payment.public_id},
            },
        }
        verified = {
            "status": "successful",
            "amount": str(order.total),
            "currency": order.currency,
        }
        with (
            patch("apps.billing.providers.flutterwave.get_fw_client") as fw,
            self.capture(execute=True),
        ):
            fw.return_value.verify_transaction.return_value = verified
            response = Client().post(
                reverse("billing:webhook"),
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_VERIF_HASH="test-hash",
            )
        order.refresh_from_db()
        return response

    # -- reports ---------------------------------------------------------------------------
    def card(self, post):
        (row,) = results_for(post)
        return row

    @property
    def period(self):
        """The last 30 days up to now; a fixed window would exclude later events."""
        return reporting.last_days(30)

    def journey(self):
        return journey(self.account, self.period)


@pytest.fixture
def shop(settings, django_capture_on_commit_callbacks):
    """The shop, with Meta's Graph API answering for the whole test: the publish task runs
    when the request's transaction commits, after the request itself has returned."""
    shop = Shop(settings, django_capture_on_commit_callbacks)
    with (
        patch.object(
            FacebookPublishProvider,
            "_post",
            lambda _self, e, **p: shop.graph.post(e, **p),
        ),
        patch.object(
            FacebookPublishProvider,
            "_get",
            lambda _self, e, **p: shop.graph.get(e, **p),
        ),
    ):
        yield shop


def _revenue(rows):
    return {r["currency"]: r["total"] for r in rows}


def _ref_code(text):
    return re.search(r"/go/([A-Z0-9]+)", text).group(1)


@pytest.fixture
def published(shop):
    """Steps 1-2: composed, submitted, approved and published through the real views."""
    shop.compose(shop.writer_client)
    post = ContentPost.objects.get(account=shop.account)
    shop.reviewer_client.post(reverse("publishing:post-approve", args=[post.pk]))
    shop.compose(
        shop.writer_client,
        post_id=str(post.pk),
        action="publish_now",
        caption=post.caption,
    )
    post.refresh_from_db()
    return post


# --- The journey ---------------------------------------------------------------------------


def test_the_journey_from_post_to_paid_order(shop):
    # 1. Compose and submit: waiting for review, with a tracked chat link in the caption.
    shop.compose(shop.writer_client)
    post = ContentPost.objects.prefetch_related("variants").get(account=shop.account)
    variant = post.variants.get(platform="facebook")
    link = TrackedLink.objects.get(variant=variant)
    assert post.approval_status == "submitted"
    assert bucket_for(post) == "awaiting_approval"
    submitted = post.approval_logs.get(action=ApprovalLog.Action.SUBMITTED)
    assert submitted.content_hash == post.compute_content_hash()
    assert f"/go/{link.code}" in variant.caption
    assert not shop.graph.calls, "nothing may publish before approval"

    # 2. Approve: the trail keeps the fingerprint of exactly what was approved.
    shop.reviewer_client.post(reverse("publishing:post-approve", args=[post.pk]))
    post.refresh_from_db()
    approved = post.approval_logs.get(action=ApprovalLog.Action.APPROVED)
    assert post.is_approval_valid()
    assert approved.performed_by == shop.reviewer
    assert (
        approved.content_hash == post.last_content_hash == post.compute_content_hash()
    )

    # 3. Publish: the real task path sends the approved caption, link included, to Meta.
    shop.compose(
        shop.writer_client,
        post_id=str(post.pk),
        action="publish_now",
        caption=post.caption,
    )
    post.refresh_from_db()
    variant.refresh_from_db()
    assert variant.status == ContentPostVariant.Status.PUBLISHED
    assert variant.publication_id
    assert post.status == ContentPost.Status.PUBLISHED
    ((endpoint, sent),) = shop.graph.calls
    assert endpoint == "feed"
    assert f"/go/{link.code}" in sent["message"]
    assert post.is_approval_valid(), "publishing must not change the approved content"
    card = shop.card(post)
    assert (card["clicks"], card["chats"], card["leads"], card["orders"]) == (
        0,
        0,
        0,
        0,
    )
    assert shop.journey()["posts"] == 1

    # 4. A customer taps the link: one real click, and WhatsApp opens with the ref code.
    res = Client().get(f"/go/{link.code}", HTTP_USER_AGENT=BROWSER)
    assert res.status_code == 302
    assert res["Location"].startswith("https://wa.me/260971111111")
    assert f"%28ref%20{link.code}%29" in res["Location"]
    click = LinkClick.objects.get(link=link)
    assert not click.is_bot
    assert shop.card(post)["clicks"] == 1
    assert shop.journey()["clicks"] == 1
    assert shop.card(post)["chats"] == 0

    # 5. The customer's first message arrives through the signed webhook with the ref.
    res, _ = shop.whatsapp_message(
        CUSTOMER, f"{link.prefill_text} (ref {link.code})", "wamid.FIRST"
    )
    assert res.status_code == 200
    conversation, contact = shop.conversation_of(CUSTOMER)
    entry = ConversationEntryPoint.objects.get(conversation=conversation)
    assert entry.kind == ConversationEntryPoint.Kind.TRACKED_LINK
    assert (entry.post_id, entry.variant_id, entry.link_id) == (
        post.pk,
        variant.pk,
        link.pk,
    )
    assert shop.card(post)["chats"] == 1
    j = shop.journey()
    assert (j["chats"], j["untracked_chats"]) == (1, 0)

    # 6. The team makes a lead from that chat: explicit, with the post as its source.
    shop.create_lead(CUSTOMER, conversation)
    lead = Lead.objects.get(account=shop.account, contact=contact)
    assert lead.conversation == conversation
    lead_attr = ConversationAttribution.objects.get(lead=lead)
    assert lead_attr.method == A.EXPLICIT
    assert (lead_attr.source_post, lead_attr.source_variant) == (post, variant)
    assert lead_attr.entry_point == entry
    assert shop.card(post)["leads"] == 1
    assert shop.journey()["leads"] == 1

    # 7. An order from the chat: counted as an order, but no revenue until it is paid.
    order = shop.create_order(CUSTOMER, Decimal("45.00"), "ZMW", conversation)
    order_attr = ConversationAttribution.objects.get(order=order)
    assert order.conversation == conversation
    assert order_attr.method == A.EXPLICIT
    assert (order_attr.source_post, order_attr.source_variant) == (post, variant)
    assert order.paid_at is None
    assert shop.card(post)["orders"] == 1
    assert shop.card(post)["revenue"] == []
    assert reporting.revenue(shop.account, shop.period) == []
    j = shop.journey()
    assert (j["orders"], j["revenue"]) == (1, [])

    # 8. Paid through Flutterwave's webhook: revenue appears once, in its own currency.
    assert shop.pay(order, tx_id=9001).status_code == 200
    assert order.status == Order.Status.PAID and order.paid_at is not None
    assert _revenue(shop.card(post)["revenue"]) == {"ZMW": Decimal("45.00")}
    assert _revenue(shop.journey()["revenue"]) == {"ZMW": Decimal("45.00")}
    assert reporting.revenue(shop.account, shop.period) == [
        {"currency": "ZMW", "orders": 1, "total": Decimal("45.00")}
    ]
    sales = reporting.sales(shop.account, shop.period)
    assert (sales["paid_orders"], sales["named"], sales["recent"]) == (1, 1, 0)
    assert shop.journey()["untracked_revenue"] == []
    assert shop.journey()["guessed_revenue"] == []


# --- Negative controls ---------------------------------------------------------------------


def test_an_organic_chat_stays_unattributed(shop, published):
    res, _ = shop.whatsapp_message(ORGANIC_CUSTOMER, "Hi, do you deliver to Kitwe?")
    assert res.status_code == 200
    conversation, _ = shop.conversation_of(ORGANIC_CUSTOMER)
    shop.create_lead(ORGANIC_CUSTOMER, conversation)
    order = shop.create_order(ORGANIC_CUSTOMER, Decimal("30.00"), "USD", conversation)
    shop.pay(order, tx_id=9101)

    assert not ConversationEntryPoint.objects.filter(conversation=conversation).exists()
    for attr in ConversationAttribution.objects.filter(conversation=conversation):
        assert attr.source_post is None and attr.entry_point is None
    card = shop.card(published)
    assert (card["chats"], card["leads"], card["orders"], card["revenue"]) == (
        0,
        0,
        0,
        [],
    )
    j = shop.journey()
    assert (j["chats"], j["leads"], j["orders"], j["revenue"]) == (0, 0, 0, [])
    # Visible in the unattributed bucket, not dropped.
    assert j["untracked_chats"] == 1
    assert j["untracked_revenue"] == [
        {"currency": "USD", "orders": 1, "total": Decimal("30.00")}
    ]
    assert j["guessed_revenue"] == []


def test_a_click_without_a_chat_counts_only_as_a_click(shop, published):
    link = TrackedLink.objects.get(post=published)

    Client().get(f"/go/{link.code}", HTTP_USER_AGENT=BROWSER)
    Client().get(f"/go/{link.code}", HTTP_USER_AGENT="Googlebot/2.1")

    card = shop.card(published)
    assert card["clicks"] == 1, "a bot's visit is not a customer's click"
    assert (card["chats"], card["leads"], card["orders"]) == (0, 0, 0)
    j = shop.journey()
    assert (j["clicks"], j["chats"], j["leads"], j["orders"]) == (1, 0, 0, 0)
    assert not Conversation.objects.filter(account=shop.account).exists()


def test_repeated_and_duplicate_events_never_inflate_the_numbers(shop, published):
    link = TrackedLink.objects.get(post=published)
    text = f"{link.prefill_text} (ref {link.code})"

    _, body = shop.whatsapp_message(CUSTOMER, text, "wamid.SAME")
    replay, _ = shop.whatsapp_message(CUSTOMER, text, body=body)  # Meta redelivers
    shop.whatsapp_message(
        CUSTOMER, f"Me again (ref {link.code})"
    )  # used the link twice
    shop.whatsapp_message(CUSTOMER, "Still keen")
    assert replay.status_code == 200
    conversation, contact = shop.conversation_of(CUSTOMER)
    assert Message.objects.filter(conversation=conversation).count() == 3

    shop.create_lead(CUSTOMER, conversation)
    shop.create_lead(CUSTOMER, conversation)  # one open lead per customer
    order = shop.create_order(CUSTOMER, Decimal("45.00"), "ZMW", conversation)
    shop.pay(order, tx_id=9201)
    shop.pay(order, tx_id=9201, replay=True)  # Flutterwave retries the same charge
    shop.request_payment(order)  # someone asks a paid customer to pay again

    order.refresh_from_db()
    assert order.status == Order.Status.PAID, "a paid order is never reopened"
    assert Lead.objects.filter(contact=contact).count() == 1
    assert order.payments.count() == 1
    assert order.payments.filter(status="succeeded").count() == 1
    card = shop.card(published)
    assert (card["chats"], card["leads"], card["orders"]) == (1, 1, 1)
    assert _revenue(card["revenue"]) == {"ZMW": Decimal("45.00")}
    assert reporting.revenue(shop.account, shop.period) == [
        {"currency": "ZMW", "orders": 1, "total": Decimal("45.00")}
    ]


def test_revenue_is_kept_per_currency_and_never_summed(shop, published):
    link = TrackedLink.objects.get(post=published)
    shop.whatsapp_message(CUSTOMER, f"Hi (ref {link.code})")
    conversation, _ = shop.conversation_of(CUSTOMER)
    zmw = shop.create_order(CUSTOMER, Decimal("45.00"), "ZMW", conversation)
    usd = shop.create_order(CUSTOMER, Decimal("20.00"), "USD", conversation)
    shop.pay(zmw, tx_id=9301)
    shop.pay(usd, tx_id=9302)

    expected = {"USD": Decimal("20.00"), "ZMW": Decimal("45.00")}
    assert _revenue(shop.card(published)["revenue"]) == expected
    assert _revenue(shop.journey()["revenue"]) == expected
    assert _revenue(reporting.revenue(shop.account, shop.period)) == expected
    for rows in (
        shop.card(published)["revenue"],
        shop.journey()["revenue"],
        reporting.revenue(shop.account, shop.period),
    ):
        assert len(rows) == 2, "one row per currency, no combined total"


def test_a_guessed_conversation_is_never_a_tracked_post_conversion(shop, published):
    link = TrackedLink.objects.get(post=published)
    shop.whatsapp_message(CUSTOMER, f"Hi (ref {link.code})")
    conversation, _ = shop.conversation_of(CUSTOMER)

    # Made from the Orders page without naming the chat: the recent-chat fallback.
    order = shop.create_order(CUSTOMER, Decimal("60.00"), "ZMW")
    shop.pay(order, tx_id=9401)

    attr = ConversationAttribution.objects.get(order=order)
    assert attr.method == A.RECENT_CONVERSATION
    assert attr.conversation == conversation
    assert attr.source_post is None and attr.entry_point is None
    card = shop.card(published)
    assert (card["chats"], card["orders"], card["revenue"]) == (1, 0, [])
    j = shop.journey()
    assert (j["orders"], j["revenue"]) == (0, [])
    # Its own bucket: neither a post's sale nor lost from the report.
    assert j["guessed_revenue"] == [
        {"currency": "ZMW", "orders": 1, "total": Decimal("60.00")}
    ]
    assert j["untracked_revenue"] == []
    sales = reporting.sales(shop.account, shop.period)
    assert (sales["named"], sales["recent"]) == (0, 1)


# --- Staging evidence command --------------------------------------------------------------


def test_the_evidence_command_shows_the_journey_without_secrets(shop, published):
    from io import StringIO

    from django.core.management import call_command

    link = TrackedLink.objects.get(post=published)
    Client().get(f"/go/{link.code}", HTTP_USER_AGENT=BROWSER)
    shop.whatsapp_message(CUSTOMER, f"My private question (ref {link.code})")
    conversation, _ = shop.conversation_of(CUSTOMER)
    order = shop.create_order(CUSTOMER, Decimal("45.00"), "ZMW", conversation)
    shop.pay(order, tx_id=9501)
    shop.whatsapp_message(ORGANIC_CUSTOMER, "Another private question")

    out = StringIO()
    call_command(
        "journey_evidence", account=shop.account.slug, post=published.pk, stdout=out
    )
    text = out.getvalue()
    data = json.loads(text)

    assert [c["status"] for c in data["channels"]] == ["published"]
    assert data["tracked_links"][0]["clicks"] == 1
    tracked, organic = data["conversations"]
    assert tracked["entry_points"][0]["post_id"] == published.pk
    assert organic["entry_points"] == []
    assert {a["method"] for a in data["attributions"]} == {"explicit"}
    assert data["reports"]["journey"]["untracked_chats"] == 1
    assert data["reports"]["revenue"] == [
        {"currency": "ZMW", "orders": 1, "total": "45.00"}
    ]
    # Nothing secret or personal.
    for secret in (
        "page-token",
        "wa-token",
        "test_app_secret",
        "private question",
        CUSTOMER,
        ORGANIC_CUSTOMER,
    ):
        assert secret not in text
    assert tracked["customer"] == "…4567"


def test_the_evidence_command_cannot_read_another_workspace(shop, published):
    from django.core.management import CommandError, call_command

    other = Account.objects.create(company_name="Other", slug="other")

    with pytest.raises(CommandError, match="No such post"):
        call_command("journey_evidence", account=other.slug, post=published.pk)


def test_the_evidence_lists_every_record_behind_the_reported_revenue(shop, published):
    """An order created before the window and paid inside it is in the revenue, so its
    row, its attribution and its chat must be in the evidence too."""
    from datetime import timedelta
    from io import StringIO

    from django.core.management import call_command

    link = TrackedLink.objects.get(post=published)
    shop.whatsapp_message(CUSTOMER, f"Hi (ref {link.code})")
    conversation, _ = shop.conversation_of(CUSTOMER)
    order = shop.create_order(CUSTOMER, Decimal("45.00"), "ZMW", conversation)
    old = timezone.now() - timedelta(days=35)
    Order.objects.filter(pk=order.pk).update(created_at=old)
    ConversationAttribution.objects.filter(order=order).update(attributed_at=old)
    Message.objects.filter(conversation=conversation).update(timestamp=old)
    shop.pay(order, tx_id=9601)

    out = StringIO()
    with patch(
        "apps.conversations.reporting.sales", wraps=reporting.sales
    ) as sales_spy:
        call_command(
            "journey_evidence", account=shop.account.slug, post=published.pk, stdout=out
        )
    data = json.loads(out.getvalue())

    assert data["reports"]["revenue"] == [
        {"currency": "ZMW", "orders": 1, "total": "45.00"}
    ]
    assert [o["public_id"] for o in data["orders"]] == [order.public_id]
    assert [a["subject_id"] for a in data["attributions"]] == [order.public_id]
    assert [c["public_id"] for c in data["conversations"]] == [conversation.public_id]
    assert sales_spy.call_count == 1
