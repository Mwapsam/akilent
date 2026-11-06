"""The details panel loads on its own beside the conversation, and its actions answer with the
panel plus whatever else on screen they change; a long thread opens on its latest messages and
loads older ones on request."""

import datetime
import json
from urllib.parse import unquote

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, FollowUp, Message
from apps.conversations.views import THREAD_WINDOW

pytestmark = pytest.mark.django_db

PANEL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "cv-panel"}
PANE = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "conversation-pane-inner"}


@pytest.fixture
def chat(client):
    user = User.objects.create_user("o", "o@example.com", "pw", first_name="Ola")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    contact = Contact.objects.create(
        account=account, phone="+260971111111", first_name="Thandi"
    )
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP
    )
    Message.objects.create(
        account=account,
        conversation=conversation,
        direction="inbound",
        body="Hello",
        timestamp=timezone.now(),
    )
    return client, account, conversation


def _url(conversation):
    return f"/inbox/{conversation.public_id}/"


def test_the_pane_leaves_the_details_to_load_beside_it(chat):
    client, _, conversation = chat
    html = client.get(_url(conversation), **PANE).content.decode()

    assert f'hx-get="/inbox/{conversation.public_id}/panel/"' in html
    assert 'hx-trigger="intersect once"' in html
    assert "data-panel-followup" not in html  # the panel's content isn't in the pane


def test_the_panel_loads_as_a_partial_that_is_never_cached(chat):
    client, _, conversation = chat
    resp = client.get(f"/inbox/{conversation.public_id}/panel/", HTTP_HX_REQUEST="true")

    body = resp.content.decode()
    assert "data-panel-followup" in body and "<html" not in body
    assert resp["Cache-Control"] == "no-store"
    assert "HX-Target" in resp["Vary"]
    assert "HX-Trigger" not in resp  # a plain load changes nothing in the list


def test_without_scripts_the_panel_is_a_page_of_its_own(chat):
    client, _, conversation = chat
    pane = client.get(_url(conversation)).content.decode()
    assert f'<noscript><a href="/inbox/{conversation.public_id}/panel/"' in pane

    page = client.get(f"/inbox/{conversation.public_id}/panel/").content.decode()
    assert "<html" in page and "data-panel-followup" in page
    assert "Back to the conversation" in page
    assert 'hx-target="#cv-panel"' not in page  # its forms post and come back, plainly


@pytest.mark.parametrize(
    ("data", "toast"),
    [
        ({"action": "pause_automation", "hours": "4"}, "paused"),
        ({"action": "add_note", "body": "Wants the blue one"}, "Note added"),
        ({"action": "create_followup", "when": "tomorrow"}, "Follow-up"),
    ],
)
def test_a_panel_action_answers_with_the_panel_and_what_else_it_changed(
    chat, data, toast
):
    client, _, conversation = chat
    resp = client.post(_url(conversation), data, **PANEL)

    body = resp.content.decode()
    assert resp.status_code == 200
    assert "data-panel-followup" in body
    assert 'class="cv-wrap"' not in body  # the panel only: the thread doesn't reload
    # The composer's Next steps, swapped out of band (a follow-up shows its dot there).
    assert 'id="cv-next"' in body and 'hx-swap-oob="true"' in body
    assert resp["HX-Trigger"] == "inbox-refresh"  # the row in the list
    # What happened, said in place (X-Toast is "type|message", the message URL-encoded).
    assert toast.lower() in unquote(resp["X-Toast"]).lower()


def test_a_follow_up_set_in_the_panel_shows_in_the_composer_at_once(chat):
    client, _, conversation = chat
    resp = client.post(
        _url(conversation), {"action": "create_followup", "when": "tomorrow"}, **PANEL
    )

    body = resp.content.decode()
    next_steps = body[body.index('id="cv-next"') :]
    assert "data-composer-followup" in next_steps  # "Follow-up tomorrow 09:00"
    assert FollowUp.objects.filter(
        conversation=conversation, done_at__isnull=True
    ).exists()


def test_a_follow_up_done_from_the_panel_comes_back_as_the_panel(chat):
    client, account, conversation = chat
    f = FollowUp.objects.create(
        account=account,
        contact=conversation.contact,
        conversation=conversation,
        due_at=timezone.now(),
    )
    resp = client.post(
        f"/inbox/followups/{f.pk}/complete/",
        {"next": _url(conversation)},
        follow=True,
        **PANEL,
    )

    body = resp.content.decode()
    assert "data-panel-followup" in body and 'id="cv-next"' in body
    assert resp["HX-Trigger"] == "inbox-refresh"
    f.refresh_from_db()
    assert f.done_at is not None


# --- A long thread ---------------------------------------------------------------------


def _long(account, conversation, n):
    now = timezone.now()
    Message.objects.bulk_create(
        Message(
            account=account,
            conversation=conversation,
            direction="outbound" if k % 2 else "inbound",
            body=f"m{k}",
            timestamp=now - datetime.timedelta(minutes=n - k),
            status="read" if k % 2 else "",
        )
        for k in range(n)
    )


def _config(html):
    import re

    return json.loads(
        re.search(r'id="chat-config"[^>]*>(.*?)</script>', html, re.S).group(1)
    )


def test_earlier_messages_load_in_pages_just_before_the_oldest_shown(chat):
    client, account, conversation = chat
    Message.objects.filter(conversation=conversation).delete()
    _long(account, conversation, 130)
    Conversation.objects.filter(pk=conversation.pk).update(is_unread=False)
    cfg = _config(client.get(_url(conversation), **PANE).content.decode())
    oldest = cfg["messages"][0]

    page = client.get(
        f"/inbox/{conversation.public_id}/messages/?before={oldest['id']}",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    ).json()

    bodies = [m["body"] for m in page["messages"]]
    assert bodies == [
        f"m{k}" for k in range(130 - 2 * THREAD_WINDOW, 130 - THREAD_WINDOW)
    ]
    assert page["hasEarlier"] is True
    last = client.get(
        f"/inbox/{conversation.public_id}/messages/?before={page['messages'][0]['id']}",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    ).json()
    assert [m["body"] for m in last["messages"]] == [f"m{k}" for k in range(30)]
    assert last["hasEarlier"] is False


def test_reading_earlier_messages_doesnt_mark_the_chat_read(chat):
    client, account, conversation = chat
    _long(account, conversation, 60)
    first = Message.objects.filter(conversation=conversation).order_by("-timestamp")[5]
    Conversation.objects.filter(pk=conversation.pk).update(is_unread=True)

    client.get(
        f"/inbox/{conversation.public_id}/messages/?before={first.pk}",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    conversation.refresh_from_db()
    assert conversation.is_unread is True


def test_an_unread_run_longer_than_the_window_opens_from_its_first_message(chat):
    """The customer wrote 70 times since the business last spoke: all 70 show, so the
    first unread message is on screen."""
    client, account, conversation = chat
    Message.objects.filter(conversation=conversation).delete()
    now = timezone.now()
    Message.objects.create(
        account=account,
        conversation=conversation,
        direction="outbound",
        body="our reply",
        status="read",
        timestamp=now - datetime.timedelta(hours=3),
    )
    Message.objects.bulk_create(
        Message(
            account=account,
            conversation=conversation,
            direction="inbound",
            body=f"u{k}",
            timestamp=now - datetime.timedelta(minutes=100 - k),
        )
        for k in range(70)
    )
    Conversation.objects.filter(pk=conversation.pk).update(is_unread=True)

    cfg = _config(client.get(_url(conversation), **PANE).content.decode())

    assert len(cfg["messages"]) == 70
    assert cfg["messages"][0]["body"] == "u0"
