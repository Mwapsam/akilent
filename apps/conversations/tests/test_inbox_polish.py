"""Inbox P0 polish: the two-pane view stays coherent, pane actions update in place,
the poll is cheap, and teammates see each other typing.

The two-pane inbox pushes the conversation's URL into the address bar, so every inbox
link must be absolute; a pane action must answer with the pane itself, not a redirect
that throws away the list beside it.
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import presence
from apps.conversations.models import Conversation
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact

BACKGROUND = {"HTTP_HX_REQUEST": "true"}
PANE = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "conversation-pane-inner"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw", first_name="Una")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.fixture
def teammate(logged_in):
    _, account, _ = logged_in
    user = User.objects.create_user("sam", "sam@example.com", "pw", first_name="Sam")
    Membership.objects.create(user=user, account=account, role=Membership.Role.MEMBER)
    return user


def _inbound(account, phone="+260971234567", wamid="wamid.POLISH", **contact_fields):
    contact = Contact.objects.create(account=account, phone=phone, **contact_fields)
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number=phone, contact=contact
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    log = MessageLog.objects.create(
        account=account,
        conversation=wa_conversation,
        contact=wa_contact,
        message_id=wamid,
        direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT,
        content="Do you have the blue dress?",
        status=MessageLog.Status.DELIVERED,
        timestamp=timezone.now(),
    )
    return record_inbound_whatsapp_message(
        contact=contact,
        wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation,
        message_log=log,
    )


@pytest.fixture
def conversation(logged_in):
    _, account, _ = logged_in
    return _inbound(account)


def _hrefs(html):
    return [h.replace("&amp;", "&") for h in re.findall(r'href="([^"]*)"', html)]


# --- Links survive the two-pane view ----------------------------------------------


@pytest.mark.django_db
def test_poll_url_keeps_every_filter(logged_in, conversation):
    client, _, _ = logged_in
    html = client.get(
        "/inbox/?view=missed&channel=whatsapp&overdue=1&page=1"
    ).content.decode()
    poll = re.search(r'hx-get="([^"]*/inbox/feed/[^"]*)"', html).group(1)
    poll = poll.replace("&amp;", "&")
    for param in ("view=missed", "channel=whatsapp", "overdue=1", "page=1"):
        assert param in poll


@pytest.mark.django_db
def test_inbox_links_are_absolute_and_never_carry_the_digest(logged_in, conversation):
    client, _, _ = logged_in
    # Rendered by the poll, whose own query carries the digest h=.
    html = client.get(
        "/inbox/feed/?view=all&channel=whatsapp&h=abc123", **BACKGROUND
    ).content.decode()
    inbox_links = [h for h in _hrefs(html) if "view=" in h]
    assert inbox_links
    for href in inbox_links:
        assert href.startswith("/inbox/?"), href
        assert "h=" not in href
    tabs = [h for h in inbox_links if "channel=whatsapp" in h]
    assert len(tabs) == 6  # every tab keeps the channel filter
    clear = [h for h in inbox_links if "channel=" not in h]
    assert len(clear) == 1, "expected one absolute Clear filter link"
    assert (
        'action="/inbox/"' in html
    )  # the filter form posts to the inbox, not the page URL


@pytest.mark.django_db
def test_pagination_links_are_absolute(logged_in):
    client, account, _ = logged_in
    for i in range(35):
        _inbound(account, phone=f"+2609700000{i:02d}", wamid=f"wamid.P{i}")
    html = client.get(
        "/inbox/feed/?view=all&overdue=&h=zz", **BACKGROUND
    ).content.decode()
    nxt = [h for h in _hrefs(html) if "page=2" in h]
    assert nxt and nxt[0].startswith("/inbox/?")
    assert "h=" not in nxt[0]


# --- Pane actions answer with the pane -------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data, toast",
    [
        ({"action": "assign"}, "Assigned to you."),
        ({"action": "assign", "assignee": "none"}, "Unassigned."),
        ({"action": "add_note", "body": "Call back after 5"}, "Note added."),
        ({"action": "close", "resolution": ""}, "Closed."),
        ({"action": "create_followup", "when": "tomorrow"}, "Follow-up scheduled."),
    ],
)
def test_pane_action_returns_the_pane_with_a_toast(
    logged_in, conversation, data, toast
):
    client, _, _ = logged_in
    resp = client.post(f"/inbox/{conversation.public_id}/", data, **PANE)
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'class="cv-wrap"' in body and "<html" not in body
    from urllib.parse import unquote

    assert toast in unquote(resp["X-Toast"])
    assert resp["HX-Trigger"] == "inbox-refresh"


@pytest.mark.django_db
def test_pane_action_error_is_a_danger_toast(logged_in, conversation):
    client, _, _ = logged_in
    resp = client.post(
        f"/inbox/{conversation.public_id}/",
        {"action": "close", "resolution": "nonsense"},
        **PANE,
    )
    assert resp.status_code == 200
    assert resp["X-Toast"].startswith("danger|")
    assert "HX-Trigger" not in resp


@pytest.mark.django_db
def test_a_normal_post_still_redirects(logged_in, conversation):
    client, _, _ = logged_in
    resp = client.post(f"/inbox/{conversation.public_id}/", {"action": "assign"})
    assert resp.status_code == 302


@pytest.mark.django_db
def test_tag_add_from_the_pane_comes_back_as_the_pane(logged_in, conversation):
    client, _, _ = logged_in
    resp = client.post(
        f"/contacts/{conversation.contact.public_id}/tags/add/",
        {"tag": "vip", "next": f"/inbox/{conversation.public_id}/"},
        follow=True,
        **PANE,
    )
    body = resp.content.decode()
    assert 'class="cv-wrap"' in body and "<html" not in body
    assert "vip" in body


@pytest.mark.django_db
def test_pane_forms_post_to_the_conversation_and_target_the_pane(
    logged_in, conversation
):
    client, _, _ = logged_in
    pane = client.get(f"/inbox/{conversation.public_id}/", **PANE).content.decode()
    url = f"/inbox/{conversation.public_id}/"
    assert f'action="{url}" hx-post="{url}"' in pane
    assert 'hx-target="#conversation-pane-inner"' in pane
    assert 'onchange="this.form.submit()"' not in pane  # no auto-submitting selects
    page = client.get(url).content.decode()
    assert f'hx-post="{url}"' not in page  # the standalone page keeps plain forms


# --- Names and titles --------------------------------------------------------------


@pytest.mark.django_db
def test_display_name_falls_back_to_phone_then_email(logged_in):
    client, account, _ = logged_in
    phone_only = _inbound(account, phone="+260970000111", wamid="wamid.A")
    assert phone_only.contact.display_name == "+260970000111"
    page = client.get(f"/inbox/{phone_only.public_id}/").content.decode()
    assert "<title>+260970000111 — Inbox" in page
    assert Contact(email="ana@example.com").display_name == "ana@example.com"
    assert Contact(first_name="Ana", phone="+1").display_name == "Ana"
    assert Contact().display_name == "Unknown customer"


# --- Cheap poll ----------------------------------------------------------------------


@pytest.mark.django_db
def test_feed_query_count_does_not_grow_with_rows(
    logged_in, django_assert_max_num_queries
):
    client, account, _ = logged_in
    for i in range(3):
        _inbound(account, phone=f"+2609711111{i:02d}", wamid=f"wamid.Q{i}")
    client.get("/inbox/feed/?view=all", **BACKGROUND)  # warm-up (session, etc.)
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as few:
        client.get("/inbox/feed/?view=all", **BACKGROUND)
    for i in range(3, 15):
        _inbound(account, phone=f"+2609711111{i:02d}", wamid=f"wamid.Q{i}")
    with django_assert_max_num_queries(len(few.captured_queries)):
        client.get("/inbox/feed/?view=all", **BACKGROUND)


# --- Typing presence -----------------------------------------------------------------


@pytest.mark.django_db
def test_typing_ping_queues_one_customer_indicator_per_window(logged_in, conversation):
    client, _, _ = logged_in
    with (
        patch("apps.conversations.views._window_is_open", return_value=True),
        patch("apps.conversations.tasks.send_typing_indicator.delay") as delay,
    ):
        for _ in range(3):
            assert client.post(f"/inbox/{conversation.public_id}/typing/").json()["ok"]
    delay.assert_called_once_with(conversation.pk)


@pytest.mark.django_db
def test_no_customer_indicator_for_a_closed_conversation(logged_in, conversation):
    client, _, _ = logged_in
    conversation.status = Conversation.Status.CLOSED
    conversation.save(update_fields=["status"])
    with (
        patch("apps.conversations.views._window_is_open", return_value=True),
        patch("apps.conversations.tasks.send_typing_indicator.delay") as delay,
    ):
        client.post(f"/inbox/{conversation.public_id}/typing/")
    delay.assert_not_called()


@pytest.mark.django_db
def test_no_customer_indicator_outside_the_window(logged_in, conversation):
    client, _, _ = logged_in
    with (
        patch("apps.conversations.views._window_is_open", return_value=False),
        patch("apps.conversations.tasks.send_typing_indicator.delay") as delay,
    ):
        client.post(f"/inbox/{conversation.public_id}/typing/")
    delay.assert_not_called()


@pytest.mark.django_db
def test_whatsapp_indicator_uses_the_latest_inbound_message(logged_in, conversation):
    from apps.conversations.tasks import send_typing_indicator

    provider = MagicMock()
    provider.send_typing_indicator.return_value = MagicMock(success=True)
    with patch("apps.whatsapp.tasks._get_provider_for_account", return_value=provider):
        assert send_typing_indicator(conversation.pk) is True
    provider.send_typing_indicator.assert_called_once_with("wamid.POLISH")


def test_whatsapp_payload_carries_the_typing_indicator():
    from apps.whatsapp.providers.meta import MetaCloudAPIProvider

    provider = MetaCloudAPIProvider.__new__(MetaCloudAPIProvider)
    provider.phone_number_id = "123"
    provider._url = lambda path: f"https://graph.example/{path}"
    provider._session = MagicMock()
    assert provider.send_typing_indicator("wamid.X").success
    payload = provider._session.post.call_args.kwargs["json"]
    assert payload["message_id"] == "wamid.X"
    assert payload["typing_indicator"] == {"type": "text"}


def test_instagram_payload_is_typing_on():
    from apps.instagram.providers.meta import MetaInstagramProvider

    provider = MetaInstagramProvider(access_token="t", instagram_account_id="ig1")
    with patch.object(provider, "_post", return_value={}) as post:
        assert provider.send_typing("igsid-9") is True
    endpoint, payload = post.call_args.args
    assert endpoint == "ig1/messages"
    assert payload == {"recipient": {"id": "igsid-9"}, "sender_action": "typing_on"}


@pytest.mark.django_db
def test_teammate_typing_shows_in_the_feed_never_yourself(
    logged_in, teammate, conversation
):
    client, _, _ = logged_in
    from django.test import Client

    sam = Client()
    sam.force_login(teammate)
    with patch("apps.conversations.tasks.send_typing_indicator.delay"):
        sam.post(f"/inbox/{conversation.public_id}/typing/")
        client.post(f"/inbox/{conversation.public_id}/typing/")

    mine = client.get(f"/inbox/{conversation.public_id}/messages/").json()["presence"]
    assert mine == {"typing": ["Sam"], "viewing": []}
    sams = sam.get(f"/inbox/{conversation.public_id}/messages/").json()["presence"]
    assert sams["typing"] == ["Una"]


@pytest.mark.django_db
def test_presence_lapses_with_its_ttl(logged_in, teammate, conversation):
    presence.mark_typing(conversation.pk, teammate.pk)
    members = [teammate]
    assert presence.for_conversation(conversation.pk, members)["typing"] == ["Sam"]
    cache.delete(f"presence:type:{conversation.pk}:{teammate.pk}")
    assert presence.for_conversation(conversation.pk, members) == {
        "typing": [],
        "viewing": ["Sam"],
    }
    cache.clear()
    assert presence.for_conversation(conversation.pk, members) == {
        "typing": [],
        "viewing": [],
    }


@pytest.mark.django_db
def test_inbox_row_shows_a_teammate_typing(logged_in, teammate, conversation):
    client, _, _ = logged_in
    presence.mark_typing(conversation.pk, teammate.pk)
    html = client.get("/inbox/feed/?view=all", **BACKGROUND).content.decode()
    assert "Sam is typing" in html
