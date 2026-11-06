"""Inbox speed: each request's query budget, at counts that don't grow with the number of
conversations or the length of a thread.

On production every query is a round trip to the database (about 10 ms), so the budget is in
queries: a template change that adds a few fails here before anyone feels it. The inbox is
opened all day and switched between far more often than Content Studio's tabs.
"""

import datetime

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, FollowUp, Message

pytestmark = pytest.mark.django_db

BACKGROUND = {"HTTP_HX_REQUEST": "true"}
PANE = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "conversation-pane-inner"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def seed(account, user, *, conversations=30, thread=60):
    """``conversations`` chats of three messages each, open and closed, some assigned, plus
    one long chat of ``thread`` messages with an open follow-up (returned)."""
    now = timezone.now()
    contacts = Contact.objects.bulk_create(
        Contact(account=account, phone=f"+2609700{i:05d}", first_name=f"Customer {i}")
        for i in range(conversations + 1)
    )
    convs = Conversation.objects.bulk_create(
        Conversation(
            account=account,
            contact=c,
            channel=Conversation.Channel.WHATSAPP,
            status=Conversation.Status.CLOSED
            if i % 5 == 4
            else Conversation.Status.OPEN,
            assigned_to=user if i % 3 == 0 else None,
            is_unread=i % 2 == 0,
            last_message_at=now - datetime.timedelta(hours=i),
        )
        for i, c in enumerate(contacts)
    )
    rows = []
    for i, conv in enumerate(convs[:-1]):
        at = now - datetime.timedelta(hours=i)
        for k, direction in enumerate(["inbound", "outbound", "inbound"]):
            rows.append(
                Message(
                    account=account,
                    conversation=conv,
                    direction=direction,
                    body=f"Message {k} in chat {i}",
                    timestamp=at - datetime.timedelta(minutes=10 - k),
                    status="delivered" if direction == "outbound" else "",
                )
            )
    long = convs[-1]
    for k in range(thread):
        rows.append(
            Message(
                account=account,
                conversation=long,
                direction="inbound" if k % 2 else "outbound",
                body=f"Long chat message {k}",
                timestamp=now - datetime.timedelta(minutes=thread - k),
                status="" if k % 2 else "read",
            )
        )
    Message.objects.bulk_create(rows)
    # An open follow-up on the long chat: the pane and the panel then describe it too.
    FollowUp.objects.create(
        account=account,
        contact=long.contact,
        conversation=long,
        due_at=now + datetime.timedelta(days=1),
        note="Ask if the size fits",
    )
    return long


@pytest.fixture
def inbox(client):
    user = User.objects.create_user(
        "owner", "owner@example.com", "pw", first_name="Ola"
    )
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    teammate = User.objects.create_user("sam", "sam@example.com", "pw")
    Membership.objects.create(
        user=teammate, account=account, role=Membership.Role.MEMBER
    )
    client.force_login(user)
    return client, account, user


def _queries(client, url, **headers) -> list[str]:
    """The SQL of one warm request. Read at once: Django clears its query log when the next
    request starts, so a capture read later would show that request's queries instead."""
    client.get(
        url, **headers
    )  # warm: first-request work (sessions, caches) isn't the page
    with CaptureQueriesContext(connection) as q:
        resp = client.get(url, **headers)
    assert resp.status_code == 200, resp.status_code
    return [x["sql"] for x in q.captured_queries]


def _urls(long) -> dict:
    detail = f"/inbox/{long.public_id}/"
    return {
        "list": ("/inbox/", {}),
        "pane": (detail, PANE),
        "panel": (f"/inbox/{long.public_id}/panel/", BACKGROUND),
        "full page": (detail, {}),
        "feed": ("/inbox/feed/", BACKGROUND),
        "badge": ("/inbox/followups/badge/", BACKGROUND),
    }


# Measured 2026-11-06 (Phase 0 of the inbox plan), from 33 / 38 / 62 / 29 / 4 before it. The
# plan's targets: list ≤25, pane ≤20, panel ≤20, full page ≤40, feed ≤15, badge ≤5. The panel
# is one over: its last query is the recommended action, which only a behaviour change would
# drop. Lower a number when a request gets faster; never raise one to make a change pass.
BUDGET = {
    "list": 17,
    "pane": 18,
    "panel": 21,
    "full page": 28,
    "feed": 13,
    "badge": 3,
}


@pytest.mark.parametrize("name", list(BUDGET))
def test_each_inbox_request_stays_within_its_query_budget(inbox, name):
    client, account, user = inbox
    long = seed(account, user)
    url, headers = _urls(long)[name]

    sqls = _queries(client, url, **headers)

    assert len(sqls) <= BUDGET[name], [q[:120] for q in sqls]


@pytest.mark.parametrize("name", ["list", "feed", "pane", "panel", "full page"])
def test_the_counts_dont_grow_with_conversations_or_thread_length(inbox, name):
    """Twice the conversations and five times the thread: the same number of queries."""
    client, account, user = inbox
    url, headers = _urls(seed(account, user))[name]
    small = len(_queries(client, url, **headers))
    Conversation.objects.filter(account=account).delete()
    Contact.objects.filter(account=account).delete()
    url, headers = _urls(seed(account, user, conversations=60, thread=300))[name]

    assert len(_queries(client, url, **headers)) == small


def test_a_long_thread_opens_on_its_latest_messages(inbox):
    import json
    import re

    from apps.conversations.views import THREAD_WINDOW

    client, account, user = inbox
    long = seed(account, user, thread=120)
    Conversation.objects.filter(pk=long.pk).update(is_unread=False)

    html = client.get(f"/inbox/{long.public_id}/", **PANE).content.decode()
    cfg = json.loads(
        re.search(r'id="chat-config"[^>]*>(.*?)</script>', html, re.S).group(1)
    )

    assert len(cfg["messages"]) == THREAD_WINDOW
    assert cfg["hasEarlier"] is True
    assert cfg["messages"][-1]["body"] == "Long chat message 119"
