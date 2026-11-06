"""Inbox Phase 6: the details panel answers who, where it stands, and what next.

- the same sections in the same order on every chat, shrinking to a line when empty;
- the name once, "Only your team sees these" once, the email only when there is one;
- Status rows: the conversation's state with Close or Reopen, who has it, the reply window,
  automations on or paused, and the account problem the composer banner shows;
- Next step: the follow-up (or one line to set one) and the latest note;
- Customer value: where they stand, orders with what they've paid, tags;
- Activity folded, with earlier conversations loaded when it opens;
- every action still a form on the panel's own page, where scripts don't run.

Folding, Esc, the docked column above the fold and touch sizes are checked in the browser by
tools/qa/inbox_qa.mjs (QA_PANEL).
"""

import datetime
import re
from urllib.parse import unquote

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import AccountFeatureOverride
from apps.contacts.models import Contact
from apps.conversations.models import (
    Conversation,
    ConversationNote,
    FollowUp,
    Message,
)

pytestmark = pytest.mark.django_db

BG = {"HTTP_HX_REQUEST": "true"}
PANEL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "cv-panel"}
SECTIONS = ["who", "status", "next", "value", "activity", "danger"]


@pytest.fixture
def shop(client):
    user = User.objects.create_user("o", "o@example.com", "pw", first_name="Ola")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


def _chat(account, *, hours_ago=1, channel="whatsapp", email="", out_after=None):
    contact = Contact.objects.create(
        account=account,
        first_name="Thandi",
        phone=f"+26097{Contact.objects.count():07d}",
        **({"email": email} if email else {}),
    )
    chat = Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )
    sent = timezone.now() - datetime.timedelta(hours=hours_ago)
    Message.objects.create(
        account=account,
        conversation=chat,
        direction="inbound",
        body="Hi",
        timestamp=sent,
    )
    if out_after is not None:
        Message.objects.create(
            account=account,
            conversation=chat,
            direction="outbound",
            body="Hello",
            status="delivered",
            timestamp=sent + datetime.timedelta(minutes=out_after),
        )
    return chat


def _panel(client, chat):
    return client.get(f"/inbox/{chat.public_id}/panel/", **BG).content.decode()


def _row(panel, name):
    start = panel.index(f'data-panel-row="{name}"')
    end = re.compile(r'data-panel-(row|section|fold)="').search(panel, start + 1)
    return panel[start : end.start() if end else None]


# --- Sections and their order ----------------------------------------------------------


def test_the_sections_come_in_one_order_on_a_full_chat_and_an_empty_one(shop):
    client, account, user = shop
    empty = _chat(account)
    full = _chat(account, email="t@example.com")
    FollowUp.objects.create(
        account=account,
        contact=full.contact,
        conversation=full,
        due_at=timezone.now() + datetime.timedelta(days=1),
    )
    ConversationNote.objects.create(
        account=account, conversation=full, author=user, body="Prefers mornings"
    )
    for chat in (empty, full):
        panel = _panel(client, chat)
        found = re.findall(r'data-panel-section="(\w+)"', panel)
        assert found == SECTIONS

    panel = _panel(client, empty)
    assert ">No follow-up<" in panel and ">Set one<" in panel
    assert ">No notes<" in panel and ">Add note<" in panel
    assert ">No tags<" in panel


def test_the_name_and_the_team_only_line_are_said_once(shop):
    client, account, _ = shop
    panel = _panel(client, _chat(account))
    shown = re.sub(r'data-confirm="[^"]*"', "", panel)
    assert shown.count("Thandi") == 1
    assert 'cv-field-label">Name' not in panel
    assert panel.count("Only your team sees these") == 1
    assert "Only your team sees this" not in panel


def test_the_email_shows_only_when_there_is_one(shop):
    client, account, _ = shop
    assert "mailto:" not in _panel(client, _chat(account))
    assert 'href="mailto:t@example.com"' in _panel(
        client, _chat(account, email="t@example.com")
    )


def test_the_header_says_how_long_and_how_often(shop):
    client, account, _ = shop
    chat = _chat(account)
    Conversation.objects.create(
        account=account, contact=chat.contact, channel="instagram"
    )
    panel = _panel(client, chat)
    since = timezone.localtime(chat.contact.first_seen).strftime("%b %Y")
    assert f"Customer since {since} · 2 conversations ·" in panel
    assert ">View profile<" in panel


# --- Status ----------------------------------------------------------------------------


def test_status_open_waiting_on_them_and_closed_with_the_right_action(shop):
    client, account, _ = shop
    waiting_for_you = _chat(account)
    row = _row(_panel(client, waiting_for_you), "status")
    assert "Open · waiting for you" in row and 'value="close"' in row

    quiet = _chat(account, hours_ago=30, out_after=5)
    row = _row(_panel(client, quiet), "status")
    assert "Waiting on them" in row and ">Close<" in row

    closed = _chat(account)
    closed.close(resolution="resolved")
    row = _row(_panel(client, closed), "status")
    assert "Closed" in row and 'value="reopen"' in row and ">Reopen<" in row


def test_close_from_the_status_row_keeps_its_undo_and_refreshes_the_header(shop):
    client, account, _ = shop
    chat = _chat(account)
    r = client.post(f"/inbox/{chat.public_id}/", {"action": "close"}, **PANEL)
    assert r.status_code == 200
    assert "/undo-close/" in unquote(r["X-Toast"])
    assert r["HX-Trigger"] == "inbox-refresh, cv-changed"
    assert ">Reopen<" in _row(r.content.decode(), "status")


def test_assigned_says_who_with_assign_to_me_or_unassign(shop):
    client, account, user = shop
    chat = _chat(account)
    row = _row(_panel(client, chat), "assigned")
    assert "Unassigned" in row and ">Assign to me<" in row
    assert f'name="assignee" value="{user.pk}"' in row

    Conversation.objects.filter(pk=chat.pk).update(assigned_to=user)
    row = _row(_panel(client, chat), "assigned")
    assert ">You<" in row and ">Unassign<" in row and 'value="none"' in row


def test_the_reply_window_open_and_closed_with_times(shop):
    client, account, _ = shop
    row = _row(_panel(client, _chat(account, hours_ago=18)), "window")
    assert "Open · closes in 6h" in row

    row = _row(_panel(client, _chat(account, hours_ago=24 * 5)), "window")
    assert "Closed 4 days ago" in row and ">Send a template<" in row


def test_in_the_pane_open_or_closed_is_the_composers_own_window(shop):
    client, account, _ = shop
    row = _row(_panel(client, _chat(account, hours_ago=18)), "window")
    assert 'x-show="windowOpen"' in row and 'x-show="!windowOpen"' in row
    # Templates (WhatsApp's) only once the composer says the window is closed.
    send = row[row.index("data-panel-send-template") :]
    assert send.index('x-show="!windowOpen"') < send.index(">Send a template<")

    page = client.get(
        f"/inbox/{_chat(account, hours_ago=18).public_id}/panel/"
    ).content.decode()
    assert "Send a template" not in _row(page, "window")  # open: nothing to send


def test_messenger_counts_the_seven_day_window_and_email_has_none(shop):
    client, account, _ = shop
    row = _row(
        _panel(client, _chat(account, hours_ago=48, channel="facebook")), "window"
    )
    assert "Open · closes in 5 days" in row
    assert "Send a template" not in row  # templates are WhatsApp's
    assert 'data-panel-row="window"' not in _panel(
        client, _chat(account, channel="email")
    )


def test_automations_on_paused_until_and_paused_with_pause_or_resume(shop):
    client, account, _ = shop
    chat = _chat(account)
    row = _row(_panel(client, chat), "automations")
    assert ">On<" in row and "Pause for…" in row and 'value="forever"' in row

    chat.pause_automation(None)
    row = _row(_panel(client, chat), "automations")
    assert ">Paused<" in row and ">Resume<" in row

    until = timezone.now() + datetime.timedelta(hours=2)
    Conversation.objects.filter(pk=chat.pk).update(automation_paused_until=until)
    row = _row(_panel(client, chat), "automations")
    local = timezone.localtime(until)
    assert "Paused until " in row and local.strftime("%H:%M") in row
    assert ">Resume<" in row


def test_the_account_problem_line_is_the_composers_own_check(shop):
    client, account, _ = shop
    panel = _panel(client, _chat(account))
    line = panel[panel.index("data-panel-account-problem") :]
    line = line[: line.index("</p>")]
    assert 'x-show="accountProblem"' in panel
    assert "accountProblem.failureReason" in line and "failureFix.url" in line
    assert ">Fix it<" in line


# --- Next step -------------------------------------------------------------------------


def test_a_follow_up_set_shows_its_actions_and_none_set_folds_the_form(shop):
    client, account, _ = shop
    chat = _chat(account)
    panel = _panel(client, chat)
    fold = panel[panel.index('data-panel-fold="followup"') :]
    assert fold.index("<summary") < fold.index("data-panel-set")  # behind its action
    assert 'name="action" value="create_followup"' in fold

    f = FollowUp.objects.create(
        account=account,
        contact=chat.contact,
        conversation=chat,
        due_at=timezone.now() + datetime.timedelta(days=1),
    )
    panel = _panel(client, chat)
    assert 'data-panel-fold="followup"' not in panel
    assert f"/inbox/followups/{f.pk}/complete/" in panel
    assert f"/inbox/followups/{f.pk}/cancel/" in panel


def test_notes_show_the_latest_and_count_the_rest(shop):
    client, account, user = shop
    chat = _chat(account)
    for body in ("first", "second", "third"):
        ConversationNote.objects.create(
            account=account, conversation=chat, author=user, body=body
        )
    panel = _panel(client, chat)
    notes = panel[panel.index("data-panel-notes") :]
    assert notes.index("third") < notes.index("2 more") < notes.index("second")
    assert ">Add note<" in notes


# --- Customer value --------------------------------------------------------------------


def test_orders_show_the_open_one_and_what_theyve_paid(shop):
    from apps.commerce.models import Order

    client, account, _ = shop
    AccountFeatureOverride.objects.create(account=account, key="orders", grant=True)
    chat = _chat(account)
    assert "None yet" in _row(_panel(client, chat), "orders")

    for total in ("40.00", "15.50"):
        Order.objects.create(
            account=account,
            contact=chat.contact,
            status=Order.Status.PAID,
            total=total,
            currency="USD",
        )
    Order.objects.create(
        account=account,
        contact=chat.contact,
        status=Order.Status.PENDING,
        total="9.99",
        currency="USD",
    )
    row = _row(_panel(client, chat), "orders")
    assert "USD 9.99" in row and "USD 55.50 paid" in row

    Order.objects.create(
        account=account,
        contact=chat.contact,
        status=Order.Status.PAID,
        total="100",
        currency="EUR",
    )
    assert "3 paid orders" in _row(_panel(client, chat), "orders")


def test_tags_are_chips_with_add_behind_its_action(shop):
    from apps.contacts.models import Tag

    client, account, _ = shop
    chat = _chat(account)
    chat.contact.tags.add(Tag.objects.create(account=account, name="vip", slug="vip"))
    row = _row(_panel(client, chat), "tags")
    assert ">vip" in row and 'aria-label="Remove tag vip"' in row
    panel = _panel(client, chat)
    fold = panel[panel.index('data-panel-fold="tag"') :]
    assert fold.index("<summary") < fold.index('name="tag"')
    assert "Add a tag" in fold


# --- Activity --------------------------------------------------------------------------


def test_earlier_conversations_load_when_activity_opens(shop):
    client, account, _ = shop
    chat = _chat(account)
    assert 'data-panel-activity="earlier"' not in _panel(client, chat)

    older = Conversation.objects.create(
        account=account, contact=chat.contact, channel="instagram"
    )
    panel = _panel(client, chat)
    earlier = panel[panel.index('data-panel-activity="earlier"') :]
    assert 'Earlier conversations<span class="inbox-tab-count">1</span>' in earlier
    assert "?part=history" in earlier and "toggle from:closest details once" in earlier
    assert older.public_id not in panel  # not until it opens

    r = client.get(f"/inbox/{chat.public_id}/panel/?part=history", **BG)
    assert r["Cache-Control"] == "no-store"
    body = r.content.decode()
    assert f"/inbox/{older.public_id}/" in body and "Instagram" in body
    assert f"/inbox/{chat.public_id}/" not in body


def test_activity_is_folded(shop):
    client, account, _ = shop
    panel = _panel(client, _chat(account))
    activity = panel[panel.index('data-panel-section="activity"') :]
    activity = activity[: activity.index('data-panel-section="danger"')]
    assert 'data-panel-activity="automations"' in activity
    assert not re.search(r"<details[^>]*\sopen[\s>]", activity)


# --- Without scripts -------------------------------------------------------------------


def test_without_scripts_every_action_is_a_form_and_earlier_chats_are_listed(shop):
    client, account, _ = shop
    chat = _chat(account)
    older = Conversation.objects.create(
        account=account, contact=chat.contact, channel="instagram"
    )
    page = client.get(f"/inbox/{chat.public_id}/panel/").content.decode()
    assert "<html" in page and "hx-post" not in page
    for action in (
        "close",
        "assign",
        "pause_automation",
        "create_followup",
        "add_note",
    ):
        assert f'name="action" value="{action}"' in page, action
    assert "/tag/" in page or "tag_add" in page or 'name="tag"' in page
    assert f"/inbox/{older.public_id}/" in page  # listed in place, no loading
    assert "?part=history" not in page
    assert "data-panel-account-problem" not in page  # the pane's own check
