"""The inbox live-refresh, after moving off the hand-rolled fetch loop.

The old inline script re-fetched the whole body every 8s and compared the HTML
client-side so an unchanged list was never written back into the DOM. HTMX has
no equivalent, so that comparison moved server-side: the poller echoes the
digest it is displaying, and an unchanged list answers 204 (HTMX swaps nothing).
These tests pin that contract, because losing it would mean the list is rebuilt
under the user every 8 seconds.
"""

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact

HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.fixture
def open_conversation(logged_in):
    _, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact,
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    log = MessageLog.objects.create(
        account=account, conversation=wa_conversation, contact=wa_contact,
        message_id="wamid.HTMXTEST", direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT, content="Do you have the blue dress?",
        status=MessageLog.Status.DELIVERED, timestamp=timezone.now(),
    )
    return record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=log,
    )


@pytest.mark.django_db
def test_htmx_gets_a_fragment_not_json(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.get("/inbox/feed/?view=all", **HTMX)
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "blue dress" in body
    assert not body.lstrip().startswith("{")  # a fragment, not a JSON envelope
    assert resp["X-Inbox-Hash"]


@pytest.mark.django_db
def test_unchanged_list_answers_204(logged_in, open_conversation):
    client, _, _ = logged_in
    first = client.get("/inbox/feed/?view=all", **HTMX)
    digest = first["X-Inbox-Hash"]

    again = client.get(f"/inbox/feed/?view=all&h={digest}", **HTMX)
    assert again.status_code == 204
    assert again.content == b""


@pytest.mark.django_db
def test_a_stale_digest_gets_the_new_body(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.get("/inbox/feed/?view=all&h=notthecurrentone", **HTMX)
    assert resp.status_code == 200
    assert "blue dress" in resp.content.decode()


@pytest.mark.django_db
def test_non_htmx_callers_keep_the_json_shape(logged_in, open_conversation):
    """The JSON envelope is still the documented response for anything that is
    not HTMX, so this stays a compatible change."""
    client, _, _ = logged_in
    resp = client.get("/inbox/feed/?view=all")
    assert resp.status_code == 200
    assert "blue dress" in resp.json()["html"]


@pytest.mark.django_db
def test_inbox_page_polls_via_htmx_and_ships_no_bespoke_loop(logged_in):
    client, _, _ = logged_in
    body = client.get("/inbox/").content.decode()
    assert "every 8s" in body
    assert "document.visibilityState" in body
    # Focus guard: a poll must not rewrite the DOM under a keyboard user.
    assert "document.activeElement" in body
    # The hand-rolled loop is gone.
    assert "data-feed-url" not in body
    assert "setTimeout(poll" not in body
