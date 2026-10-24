"""One next step at a time, from what the business has already done: conversation -> lead
-> follow-up -> payment link -> post results. Only for usable features, and "Not now"
hides a suggestion for good."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts import onboarding as ob
from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(client, settings):
    settings.WHATSAPP_ENABLED = True
    user = User.objects.create_user("o@example.com", "o@example.com", "pw")
    account = Account.objects.create(
        company_name="Shop", selected_channels=["whatsapp"]
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return account


def _usable(monkeypatch, keys):
    from apps.billing import api as billing_api

    monkeypatch.setattr(
        billing_api,
        "nav_state",
        lambda account: {
            "usable": frozenset(keys),
            "locked": frozenset(),
            "tools_off": frozenset(),
        },
    )


EVERYTHING = {"sales", "follow_ups", "orders", "content_studio"}


def _chat(account, body="How much is the dress?"):
    contact = Contact.objects.create(account=account, phone="+12025550111")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+12025550111", contact=contact
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    log = MessageLog.objects.create(
        account=account,
        conversation=wa_conversation,
        contact=wa_contact,
        message_id="wamid.NEXT",
        direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT,
        content=body,
        status=MessageLog.Status.DELIVERED,
        timestamp=timezone.now(),
    )
    conversation = record_inbound_whatsapp_message(
        contact=contact,
        wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation,
        message_log=log,
        enroll_workflows=False,
    )
    return contact, conversation


def _key(account):
    action = ob.next_action(account)
    return action["key"] if action else None


def test_nothing_to_suggest_for_a_new_workspace(owner, monkeypatch):
    _usable(monkeypatch, EVERYTHING)
    assert ob.next_action(owner) is None


def test_the_suggestions_follow_the_journey(owner, monkeypatch):
    from apps.commerce.services import create_order
    from apps.conversations.models import FollowUp
    from apps.crm.models import Lead

    _usable(monkeypatch, EVERYTHING)
    # A question with no buying words, so no lead is opened automatically.
    contact, conversation = _chat(owner, body="What time do you open?")
    assert _key(owner) == "mark_lead"

    Lead.objects.create(account=owner, contact=contact)
    assert _key(owner) == "set_followup"

    FollowUp.objects.create(
        account=owner,
        contact=contact,
        conversation=conversation,
        due_at=timezone.now(),
    )
    assert _key(owner) is None

    order = create_order(
        owner,
        contact,
        [
            {
                "product": None,
                "name": "Dress",
                "unit_price": Decimal("45"),
                "quantity": 1,
            }
        ],
    )
    action = ob.next_action(owner)
    assert action["key"] == "payment_link"
    assert action["url"] == f"/orders/{order.public_id}/"


def test_post_results_point_at_the_latest_published_post(owner, monkeypatch):
    from apps.publishing.models import ContentPost

    _usable(monkeypatch, EVERYTHING)
    post = ContentPost.objects.create(
        account=owner, platforms=["facebook"], media_type="image", status="published"
    )
    action = ob.next_action(owner)
    assert action["key"] == "post_results"
    assert action["url"] == f"/publishing/posts/{post.pk}/"


def test_never_for_a_feature_the_business_cannot_use(owner, monkeypatch):
    _usable(monkeypatch, {"follow_ups"})  # no Sales on this plan
    _chat(owner, body="What time do you open?")
    assert ob.next_action(owner) is None


def test_not_now_hides_it_for_good(owner, client, monkeypatch):
    _usable(monkeypatch, EVERYTHING)
    _chat(owner, body="What time do you open?")
    resp = client.post("/onboarding/next/mark_lead/dismiss/", {"next": "/inbox/"})
    assert resp.status_code == 302 and resp.url == "/inbox/"
    assert _key(owner) != "mark_lead"


def test_dismiss_never_redirects_off_site(owner, client):
    resp = client.post(
        "/onboarding/next/mark_lead/dismiss/", {"next": "https://evil.example/"}
    )
    assert resp.url == "/dashboard/"


def test_an_unknown_suggestion_is_ignored(owner):
    assert ob.dismiss_next_action(owner, "nope") is False


def test_the_card_shows_on_the_inbox_and_in_content_studio(owner, client, monkeypatch):
    _usable(monkeypatch, EVERYTHING)
    _chat(owner, body="What time do you open?")
    for url in ("/inbox/", "/publishing/"):
        html = client.get(url).content.decode()
        assert 'data-next-action="mark_lead"' in html, url


def test_a_paid_order_elsewhere_does_not_hide_the_payment_link(owner, monkeypatch):
    from apps.commerce.models import Payment
    from apps.commerce.services import create_order
    from apps.conversations.models import FollowUp
    from apps.crm.models import Lead

    _usable(monkeypatch, EVERYTHING)
    contact, conversation = _chat(owner, body="What time do you open?")
    Lead.objects.create(account=owner, contact=contact)
    FollowUp.objects.create(
        account=owner, contact=contact, conversation=conversation, due_at=timezone.now()
    )
    item = [
        {"product": None, "name": "Dress", "unit_price": Decimal("45"), "quantity": 1}
    ]
    paid = create_order(owner, contact, item)
    Payment.objects.create(account=owner, order=paid, amount=paid.total)
    waiting = create_order(owner, contact, item)
    action = ob.next_action(owner)
    assert action["key"] == "payment_link"
    assert action["url"] == f"/orders/{waiting.public_id}/"
