"""Inbox Phase 5D and 5E: rows, the empty pane, wording, the live feel.

- a row's quick actions (close, assign to me, follow up tomorrow) post the pane's own actions
  and are answered with their toast only (204, Close keeps its Undo) and the list re-read;
- rows carry their channel on the avatar, two lines, no channel tag of their own;
- "Waiting on them" (once "Quiet"), with its why;
- the empty pane says what needs you, from the bar's counts, and comes with every changed list;
- "New messages" goes where the customer's unread run starts.

Row height, hover, the changed-row highlight and messages sliding in are checked in the browser
by tools/qa/inbox_qa.mjs (QA_ROWS, QA_LIVE).
"""

import datetime
from urllib.parse import unquote

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, FollowUp, Message
from apps.conversations.views import _first_unread

pytestmark = pytest.mark.django_db

ROW = {"HTTP_HX_REQUEST": "true", "HTTP_X_INBOX_ROW": "1"}
FEED = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def shop(client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


def _chat(account, name="Thandi", *, minutes_ago=5, channel="whatsapp"):
    contact = Contact.objects.create(
        account=account, first_name=name, phone=f"+26097{abs(hash(name)) % 10**7:07d}"
    )
    chat = Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )
    Message.objects.create(
        account=account,
        conversation=chat,
        direction="inbound",
        body="Hi",
        timestamp=timezone.now() - datetime.timedelta(minutes=minutes_ago),
    )
    return chat


# --- Quick actions ---------------------------------------------------------------------


def test_close_from_a_row_answers_with_its_undo_and_the_list(shop):
    client, account, _ = shop
    chat = _chat(account)
    r = client.post(f"/inbox/{chat.public_id}/", {"action": "close"}, **ROW)
    assert r.status_code == 204 and r["HX-Trigger"] == "inbox-refresh"
    kind, message, undo = r["X-Toast"].split("|")
    assert unquote(message).startswith("Closed.") and "/undo-close/" in unquote(undo)
    chat.refresh_from_db()
    assert chat.status == Conversation.Status.CLOSED


def test_assign_and_follow_up_from_a_row(shop):
    client, account, user = shop
    chat = _chat(account)
    r = client.post(
        f"/inbox/{chat.public_id}/", {"action": "assign", "assignee": user.pk}, **ROW
    )
    assert r.status_code == 204 and "Assigned to you." in unquote(r["X-Toast"])
    r = client.post(
        f"/inbox/{chat.public_id}/",
        {"action": "create_followup", "when": "tomorrow"},
        **ROW,
    )
    assert r.status_code == 204
    chat.refresh_from_db()
    assert chat.assigned_to_id == user.pk
    assert FollowUp.objects.filter(conversation=chat).exists()


def test_a_refused_row_action_says_why(shop):
    client, account, _ = shop
    chat = _chat(account)
    r = client.post(
        f"/inbox/{chat.public_id}/", {"action": "close", "resolution": "nope"}, **ROW
    )
    assert r.status_code == 204 and r["X-Toast"].startswith("danger|")


# --- Rows and the bar ------------------------------------------------------------------


def test_rows_carry_the_channel_on_the_avatar_and_their_actions(shop):
    client, account, user = shop
    _chat(account, channel="instagram")
    html = client.get("/inbox/?view=all").content.decode()
    row = html[html.index('class="inbox-list"') :]
    assert 'class="inbox-avatar-channel" data-channel="instagram"' in row
    assert 'class="channel-tag' not in row
    assert 'title="Close"' in row and 'title="Assign to me"' in row
    assert 'title="Follow up tomorrow"' in row and 'tabindex="-1"' in row
    assert "X-Inbox-Row" in row


def test_the_bar_says_what_its_icons_are(shop):
    client, _, _ = shop
    html = client.get("/inbox/").content.decode()
    bar = html[html.index('id="inbox-bar"') :]
    for tip in ("Filter by channel", "Follow-ups", "Inbox set-up"):
        assert f'data-tooltip="{tip}"' in bar


# --- The empty pane --------------------------------------------------------------------


def test_the_empty_pane_says_what_needs_you_and_moves_with_the_list(shop):
    client, account, _ = shop
    _chat(account)
    page = client.get("/inbox/").content.decode()
    today = page[page.index('id="inbox-today"') :]
    assert "What needs you" in today and "Needs your reply" in today
    assert "?view=missed" in today and "Follow-ups to do" in today
    assert "<kbd>J</kbd>" in today

    feed = client.get("/inbox/feed/?view=all", **FEED).content.decode()
    assert 'id="inbox-today" hx-swap-oob="true"' in feed

    assert client.get("/inbox/followups/badge/?plain=1").content == b"0"


# --- New messages ----------------------------------------------------------------------


def test_new_messages_starts_at_the_customers_unread_run():
    def m(i, d):
        return Message(id=i, direction=d)

    thread = [m(1, "inbound"), m(2, "outbound"), m(3, "inbound"), m(4, "inbound")]
    assert _first_unread(thread, unread=True) == {"firstUnreadId": 3, "unreadCount": 2}
    assert _first_unread(thread, unread=False)["firstUnreadId"] is None
    assert _first_unread(thread[:2], unread=True)["firstUnreadId"] is None


def test_the_pane_tells_the_thread_where_unread_starts(shop):
    client, account, _ = shop
    chat = _chat(account)
    Conversation.objects.filter(pk=chat.pk).update(is_unread=True)
    pane = client.get(
        f"/inbox/{chat.public_id}/?peek=1",
        HTTP_HX_REQUEST="true",
        HTTP_HX_TARGET="conversation-pane-inner",
    ).content.decode()
    first = chat.messages.get().pk
    assert f'"firstUnreadId": {first}' in pane and '"unreadCount": 1' in pane
    assert "item.type === 'unread'" in pane and "'cv-msg--fresh': item.m.fresh" in pane
