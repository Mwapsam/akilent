"""Inbox Phase 5A: opening a conversation feels instant.

static/js/inbox_pane.js fetches a pane ahead (a hovered row, the next one after J) as a peek
with ``X-Inbox-Prefetch: 1``, keeps the last few, and names the conversation in the loading
skeleton from its row. What the server must do for that:

- a fetch ahead neither marks the conversation read (the peek) nor uses up a queued
  confirmation, which belongs to the load that shows;
- the pane is never cached by the browser or a CDN as the conversation's page.

The timings (name under 50 ms, a pane fetched ahead under 100 ms) are checked in the browser
by tools/qa/inbox_qa.mjs (QA_FAST).
"""

import pytest
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.messages.storage.base import Message as Flash
from django.contrib.messages.storage.cookie import MessageEncoder
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

pytestmark = pytest.mark.django_db

PANE = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "conversation-pane-inner"}
AHEAD = {**PANE, "HTTP_X_INBOX_PREFETCH": "1"}


@pytest.fixture
def chat(client, settings):
    settings.MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    contact = Contact.objects.create(
        account=account, first_name="Thandi", phone="+260970000001"
    )
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    Message.objects.create(
        account=account,
        conversation=conversation,
        direction="inbound",
        body="Hi",
        timestamp=timezone.now(),
    )
    Conversation.objects.filter(pk=conversation.pk).update(is_unread=True)
    return client, conversation


def _queue(client, text):
    """A confirmation waiting in the session, as a redirected form leaves one."""
    session = client.session
    session["_messages"] = MessageEncoder().encode([Flash(messages.SUCCESS, text)])
    session.save()


def test_a_fetch_ahead_leaves_the_confirmation_for_the_load_that_shows(chat):
    client, conversation = chat
    _queue(client, "Tag added.")
    url = f"/inbox/{conversation.public_id}/?peek=1"

    ahead = client.get(url, **AHEAD)
    assert ahead.status_code == 200 and "X-Toast" not in ahead
    conversation.refresh_from_db()
    assert conversation.is_unread

    shown = client.get(url, **PANE)
    assert "Tag added." in shown["X-Toast"].replace("%20", " ")


def test_the_pane_is_never_cached_as_the_page(chat):
    client, conversation = chat
    pane = client.get(f"/inbox/{conversation.public_id}/", **PANE)
    assert pane["Cache-Control"] == "no-store"
    assert {"HX-Request", "HX-Target"} <= {v.strip() for v in pane["Vary"].split(",")}


def test_the_skeleton_has_room_for_the_rows_name(chat):
    client, _ = chat
    page = client.get("/inbox/").content.decode()
    assert "data-skel-name" in page and "data-skel-avatar" in page
