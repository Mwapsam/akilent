"""Inbox Phase 4: the keyboard.

- J/K open the next or previous conversation as a peek (``?peek=1``), which doesn't mark it read;
  static/js/inbox_pane.js marks it read (``action=mark_read``) only once it has stayed open;
- E closes with an Undo that puts back what was there before: the status, the assignee and the
  close reason, recorded at close time and signed into the Undo's address, and only while
  nothing has changed since;
- the hooks the keys use (rows, the pane, Content Studio's N) and the list under "?".

The timing (1.5s, five quick J presses marking only the last) is checked in the browser by
tools/qa/inbox_qa.mjs (QA_KEYS).
"""

import datetime
from urllib.parse import unquote

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Event, Message

pytestmark = pytest.mark.django_db

PANE = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "conversation-pane-inner"}


@pytest.fixture
def shop(client):
    user = User.objects.create_user("o", "o@example.com", "pw", first_name="Ola")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


def _chat(account, name="Thandi", **kw):
    contact = Contact.objects.create(
        account=account, first_name=name, phone=f"+26097{abs(hash(name)) % 10**7:07d}"
    )
    chat = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp", **kw
    )
    Message.objects.create(
        account=account,
        conversation=chat,
        direction="inbound",
        body="Hi",
        timestamp=timezone.now() - datetime.timedelta(minutes=5),
    )
    Conversation.objects.filter(pk=chat.pk).update(is_unread=True)
    chat.refresh_from_db()
    return chat


def _undo_path(response):
    kind, message, undo = response["X-Toast"].split("|")
    return unquote(message), unquote(undo)


def _state(chat):
    chat.refresh_from_db()
    return (chat.status, chat.assigned_to_id, chat.resolution, chat.closed_at)


# --- Peek and read ---------------------------------------------------------------------


def test_a_peek_leaves_the_conversation_unread(shop):
    client, account, _ = shop
    chat = _chat(account)
    client.get(f"/inbox/{chat.public_id}/?peek=1", **PANE)
    chat.refresh_from_db()
    assert chat.is_unread


def test_staying_on_it_marks_it_read(shop):
    client, account, _ = shop
    chat = _chat(account)
    r = client.post(
        f"/inbox/{chat.public_id}/",
        {"action": "mark_read"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    chat.refresh_from_db()
    assert not chat.is_unread


def test_a_click_still_marks_it_read_at_once(shop):
    client, account, _ = shop
    chat = _chat(account)
    client.get(f"/inbox/{chat.public_id}/", **PANE)
    chat.refresh_from_db()
    assert not chat.is_unread


# --- Close and Undo --------------------------------------------------------------------


def test_close_then_undo_puts_back_status_assignee_and_reason(shop):
    client, account, user = shop
    teammate = User.objects.create_user("t", "t@example.com", "pw")
    Membership.objects.create(
        user=teammate, account=account, role=Membership.Role.MEMBER
    )
    chat = _chat(account, assigned_to=teammate)
    before = _state(chat)

    r = client.post(f"/inbox/{chat.public_id}/", {"action": "close"}, **PANE)
    message, undo = _undo_path(r)
    assert message.startswith("Closed.") and undo.startswith(
        f"/inbox/{chat.public_id}/undo-close/?t="
    )
    assert _state(chat)[0] == Conversation.Status.CLOSED
    # Something changed the assignee on close (routing, a teammate): Undo puts it back too.
    Conversation.objects.filter(pk=chat.pk).update(assigned_to=user)

    r = client.post(undo, HTTP_HX_REQUEST="true")
    assert r.status_code == 200 and "Put back" in unquote(r["X-Toast"])
    assert r["HX-Trigger"] == "inbox-refresh"
    assert _state(chat) == before
    assert Event.objects.filter(
        subject_id=str(chat.pk), type="conversation.reopened", payload__undo=True
    ).exists()


def test_undo_restores_an_earlier_close_reason(shop):
    """Closed as Won, then closed again as Lost from the menu: Undo is Won again, when it was."""
    client, account, _ = shop
    chat = _chat(account)
    chat.close(resolution=Conversation.Resolution.values[0], actor="test")
    before = _state(chat)

    other = Conversation.Resolution.values[-1]
    r = client.post(
        f"/inbox/{chat.public_id}/", {"action": "close", "resolution": other}, **PANE
    )
    assert _state(chat)[2] == other
    client.post(_undo_path(r)[1], HTTP_HX_REQUEST="true")
    assert _state(chat) == before


def test_an_undo_after_the_customer_wrote_changes_nothing(shop):
    client, account, _ = shop
    chat = _chat(account)
    undo = _undo_path(
        client.post(f"/inbox/{chat.public_id}/", {"action": "close"}, **PANE)
    )[1]
    chat.refresh_from_db()
    chat.reopen(actor="customer")  # they wrote again
    after = _state(chat)

    r = client.post(undo, HTTP_HX_REQUEST="true")
    assert "left as it is" in unquote(r["X-Toast"])
    assert _state(chat) == after


def test_an_undo_that_is_forged_or_someone_elses_does_nothing(shop, client):
    client_, account, _ = shop
    chat = _chat(account)
    undo = _undo_path(
        client_.post(f"/inbox/{chat.public_id}/", {"action": "close"}, **PANE)
    )[1]
    closed = _state(chat)

    r = client_.post(undo[:-3] + "xyz", HTTP_HX_REQUEST="true")
    assert "expired" in unquote(r["X-Toast"]) and _state(chat) == closed

    teammate = User.objects.create_user("t", "t@example.com", "pw")
    Membership.objects.create(
        user=teammate, account=account, role=Membership.Role.MEMBER
    )
    client_.force_login(teammate)
    r = client_.post(undo, HTTP_HX_REQUEST="true")
    assert "expired" in unquote(r["X-Toast"]) and _state(chat) == closed


def test_undo_is_post_only(shop):
    client, account, _ = shop
    chat = _chat(account)
    assert client.get(f"/inbox/{chat.public_id}/undo-close/?t=x").status_code == 405


# --- The hooks -------------------------------------------------------------------------


def test_the_keys_have_what_they_need_on_the_page(shop):
    client, account, user = shop
    chat = _chat(account)
    page = client.get(f"/inbox/{chat.public_id}/").content.decode()

    assert 'data-title="Thandi" data-row>' in page  # J/K, Enter
    assert f'data-me="{user.pk}"' in page  # A
    assert "data-shortcut-new" not in page  # N is Content Studio's, not the inbox's
    for line in ("R  Reply", "E  Close the conversation (with Undo)", "A  Assign"):
        assert line in page  # listed under "?"


def test_content_studio_says_where_n_goes(shop):
    client, _, _ = shop
    page = client.get("/publishing/library/").content.decode()
    assert 'data-shortcut-new="/publishing/new/"' in page
