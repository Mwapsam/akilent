"""Inbox Phase 1: each conversation's state is said plainly and the same everywhere.

- a row whose latest message didn't go says "Not sent", and the poll notices when a send fails
  after the fact;
- a failure reads in plain words, worked out from its code when shown, with where to fix it
  when the cause is the WhatsApp account;
- Closed is the stored status; an open chat gone quiet is "Quiet", not "Closed";
- views go by status, so the counts add up, and every count shows, 0 included;
- the channel filter only offers channels with conversations; search finds names, numbers and
  words from the chat.
"""

import datetime
import json
import re

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, FollowUp, Message

pytestmark = pytest.mark.django_db

BG = {"HTTP_HX_REQUEST": "true"}
PANE = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "conversation-pane-inner"}


@pytest.fixture
def shop(client):
    user = User.objects.create_user("o", "o@example.com", "pw", first_name="Ola")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


def _chat(
    account, name="Thandi", *, status="open", channel="whatsapp", phone=None, **kw
):
    contact = Contact.objects.create(
        account=account,
        first_name=name,
        phone=phone or f"+26097{abs(hash(name)) % 10**7:07d}",
    )
    return Conversation.objects.create(
        account=account, contact=contact, channel=channel, status=status, **kw
    )


def _say(conversation, body, *, direction="inbound", hours_ago=1, status="", **meta):
    return Message.objects.create(
        account=conversation.account,
        conversation=conversation,
        direction=direction,
        body=body,
        status=status or ("delivered" if direction == "outbound" else ""),
        timestamp=timezone.now() - datetime.timedelta(hours=hours_ago),
        metadata=meta,
    )


def _row(html, name):
    start = html.index(f'data-title="{name}"')
    return html[start : html.index("</li>", start)]


# --- Not sent --------------------------------------------------------------------------


def test_a_row_whose_latest_message_failed_says_not_sent_and_why(shop):
    client, account, _ = shop
    chat = _chat(account)
    _say(chat, "Hi", hours_ago=2)
    _say(
        chat, "download_app", direction="outbound", status="failed", error_code="131042"
    )

    row = _row(client.get("/inbox/").content.decode(), "Thandi")

    assert "Not sent:" in row and "download_app" in row
    assert "billing problem" in row  # the reason, on hover
    assert "You: " not in row


def test_a_send_that_fails_after_the_fact_reaches_the_list_on_the_next_poll(shop):
    """Message has no change time, so the poll's "nothing changed" check must compare what
    the list shows, which now includes whether the last send went."""
    client, account, _ = shop
    chat = _chat(account)
    _say(chat, "Hi", hours_ago=2)
    sent = _say(chat, "On its way", direction="outbound")
    first = client.get("/inbox/feed/", **BG)
    digest = first["X-Inbox-Hash"]
    assert client.get(f"/inbox/feed/?h={digest}", **BG).status_code == 204

    Message.objects.filter(pk=sent.pk).update(status="failed")

    again = client.get(f"/inbox/feed/?h={digest}", **BG)
    assert again.status_code == 200
    assert "Not sent:" in _row(again.content.decode(), "Thandi")


# --- Plain failure text ----------------------------------------------------------------


def test_every_known_code_reads_in_plain_words():
    from apps.conversations.views import _failure
    from apps.whatsapp.friendly_errors import _GRAPH_ERRORS

    for code in _GRAPH_ERRORS:
        for shape in (code, f"{code}/2494010", f"#{code}"):
            text, _ = _failure({"error_code": shape})
            assert "(error " not in text, shape


def test_an_old_message_gets_todays_wording_from_its_code(shop):
    client, account, _ = shop
    chat = _chat(account)
    _say(
        chat,
        "download_app",
        direction="outbound",
        status="failed",
        error_code="131042",
        failure_reason="This message could not be delivered. (error 131042)",
    )

    html = client.get(f"/inbox/{chat.public_id}/", **PANE).content.decode()
    cfg = json.loads(
        re.search(r'id="chat-config"[^>]*>(.*?)</script>', html, re.S).group(1)
    )
    failed = cfg["messages"][-1]

    assert "(error " not in failed["failureReason"]
    assert "payment method" in failed["failureReason"]
    assert failed["failureFix"]["label"] == "Fix the payment method in Meta"
    assert failed["failureFix"]["url"].startswith("https://business.facebook.com/")


def test_only_account_problems_get_a_fix_link():
    from apps.conversations.views import _failure

    assert _failure({"error_code": "190"})[1]["url"] == "/whatsapp/numbers/"
    assert (
        _failure({"error_code": "131026"})[1] is None
    )  # about this customer, not the account
    assert _failure({"failure_reason": "Held, not sent"}) == ("Held, not sent", None)


# --- Closed, Quiet, Reopen -------------------------------------------------------------


def test_a_closed_chat_says_closed_even_with_only_failed_sends(shop):
    client, account, _ = shop
    chat = _chat(account, status="closed")
    _say(chat, "download_app", direction="outbound", status="failed")

    html = client.get("/inbox/?view=closed").content.decode()

    assert ">Closed</span>" in _row(html, "Thandi")


def test_an_open_chat_gone_quiet_says_quiet_not_closed(shop):
    client, account, _ = shop
    chat = _chat(account)
    _say(chat, "Thanks!", hours_ago=30)
    _say(chat, "You're welcome", direction="outbound", hours_ago=29)

    row = _row(client.get("/inbox/").content.decode(), "Thandi")
    header = client.get(f"/inbox/{chat.public_id}/", **PANE).content.decode()

    assert ">Quiet</span>" in row and ">Closed</span>" not in row
    status = json.loads(
        re.search(r'id="chat-config"[^>]*>(.*?)</script>', header, re.S).group(1)
    )["statusHtml"]
    assert "Quiet" in status and "Closed" not in status


def test_a_closed_chat_offers_reopen_in_the_more_menu(shop):
    client, account, _ = shop
    chat = _chat(account, status="closed")
    _say(chat, "Hi")

    html = client.get(f"/inbox/{chat.public_id}/", **PANE).content.decode()
    reopen = html[html.index("data-menu-reopen") - 120 : html.index("data-menu-reopen")]

    assert 'x-show="!open"' in reopen and "x-cloak" not in reopen
    assert 'value="reopen"' in html[html.index("data-menu-reopen") :][:600]

    open_chat = _chat(account, "Musa")
    html = client.get(f"/inbox/{open_chat.public_id}/", **PANE).content.decode()
    tag = html[html.index("data-menu-reopen") - 120 : html.index("data-menu-reopen")]
    assert "x-cloak" in tag  # hidden until the conversation is closed


# --- Views and counts ------------------------------------------------------------------


def test_open_views_show_open_chats_and_the_counts_add_up(shop):
    client, account, user = shop
    for name in ("A", "B"):
        _say(_chat(account, name, assigned_to=user), "hi")
    _say(_chat(account, "C", status="closed", assigned_to=user), "hi")

    ctx = client.get("/inbox/").context

    assert ctx["all_count"] == 2
    assert ctx["closed_count"] == 1
    assert ctx["assigned_count"] == 2  # the closed one isn't "assigned to me" work
    assert ctx["all_count"] + ctx["closed_count"] == Conversation.objects.count()
    assert {c.contact.first_name for c in ctx["page"]} == {"A", "B"}
    assert {
        c.contact.first_name
        for c in client.get("/inbox/?view=assigned").context["page"]
    } == {"A", "B"}


def test_a_closed_chat_with_a_follow_up_due_stays_in_closed(shop):
    client, account, user = shop
    chat = _chat(account, status="closed", assigned_to=user)
    _say(chat, "hi")
    FollowUp.objects.create(
        account=account,
        contact=chat.contact,
        conversation=chat,
        due_at=timezone.now() - datetime.timedelta(minutes=5),
    )

    names = lambda url: {c.contact.first_name for c in client.get(url).context["page"]}  # noqa: E731
    assert "Thandi" not in names("/inbox/") and "Thandi" not in names(
        "/inbox/?view=assigned"
    )
    assert "Thandi" in names("/inbox/?view=closed")
    assert "Thandi" in client.get("/inbox/followups/?list=team").content.decode()


def test_every_view_shows_its_count_even_when_it_is_zero(shop):
    client, _, _ = shop
    html = client.get("/inbox/").content.decode()
    menu = html[html.index('aria-label="Inbox views"') :]
    menu = menu[: menu.index("</a>", menu.index("Closed<span"))]

    assert menu.count('class="inbox-tab-count') == 6
    assert (
        "inbox-tab-count--danger" not in menu
    )  # Missed is red only when there are some


# --- Channel filter and search ---------------------------------------------------------


def test_the_channel_filter_only_offers_channels_with_conversations(shop, settings):
    client, account, _ = shop
    _say(_chat(account), "hi")

    choices = dict(client.get("/inbox/").context["channel_choices"])

    assert "whatsapp" in choices
    assert "sms" not in choices and "webchat" not in choices
    # A filter already in force stays offered, so it can be seen and changed.
    assert "sms" in dict(client.get("/inbox/?channel=sms").context["channel_choices"])


@pytest.mark.parametrize("q", ["thandi", "0971", "blue dress"])
def test_search_finds_a_name_a_number_or_words_from_the_chat(shop, q):
    client, account, _ = shop
    _say(_chat(account, "Thandi", phone="+260971234567"), "Do you have the blue dress?")
    _say(_chat(account, "Musa", phone="+260955555555"), "Hello")

    names = {c.contact.first_name for c in client.get(f"/inbox/?q={q}").context["page"]}

    assert names == {"Thandi"}


def test_search_stays_within_the_view_and_the_poll_keeps_it(shop):
    client, account, _ = shop
    _say(_chat(account, "Thandi", status="closed"), "blue dress")
    _say(_chat(account, "Tamara"), "blue dress")

    html = client.get("/inbox/feed/?q=blue&view=all", **BG).content.decode()
    assert 'data-title="Tamara"' in html and 'data-title="Thandi"' not in html
    # The poll takes the search from the box rather than its own address.
    page = client.get("/inbox/?q=blue").content.decode()
    assert 'q: (document.getElementById("inbox-q")' in page
    assert re.search(r'hx-get="/inbox/feed/[^"]*q=', page) is None
    assert 'value="blue"' in page and "data-search" in page


def test_a_search_with_no_match_says_so(shop):
    client, account, _ = shop
    _say(_chat(account), "hi")
    html = client.get("/inbox/?q=nothing-like-this").content.decode()
    assert "No conversations match" in html
