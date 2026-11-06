"""Inbox Phase 2: room for messages, one window-closed state, the details beside the chat.

- one bar above the list (title, views, channel, Follow-ups, Set-up), the same with a
  conversation open, and the suggested next step in the empty pane instead of above the list;
- a closed reply window is one state in the composer: on WhatsApp the template picker in the
  reply box's place, an account problem first when the last send failed for one, and how to get
  a template when there are none; on Instagram the reply box switched off, saying so;
- the details are a column beside the chat where both fit (hide and resize, remembered) and a
  drawer where they don't.

What the browser shows (heights, no sideways scroll, no shift on open, drawer focus) is checked
by tools/qa/inbox_qa.mjs (QA_LAYOUT, QA_COMPOSER).
"""

import datetime

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

pytestmark = pytest.mark.django_db

PANE = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "conversation-pane-inner"}


@pytest.fixture
def shop(client):
    user = User.objects.create_user("o", "o@example.com", "pw", first_name="Ola")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _chat(account, *, channel="whatsapp", hours_ago=30):
    contact = Contact.objects.create(
        account=account, first_name="Thandi", phone="+260971234567"
    )
    chat = Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )
    Message.objects.create(
        account=account,
        conversation=chat,
        direction="inbound",
        body="Hi",
        timestamp=timezone.now() - datetime.timedelta(hours=hours_ago),
    )
    return chat


def _template(account, **kw):
    from apps.whatsapp.models import MessageTemplate

    return MessageTemplate.objects.create(
        account=account,
        name=kw.pop("name", "order_update"),
        language_code="en",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        **kw,
    )


def _pane(client, chat):
    return client.get(f"/inbox/{chat.public_id}/", **PANE).content.decode()


# --- The bar ---------------------------------------------------------------------------


def test_one_bar_heads_the_list_and_the_suggestion_waits_in_the_empty_pane(shop):
    client, account = shop
    _chat(account)
    page = client.get("/inbox/").content.decode()

    bar = page[page.index('id="inbox-bar"') :]
    bar = bar[: bar.index("</header>")]
    # The title, as Content Studio's: an "Inbox" crumb over the view's name, which is the menu.
    assert 'class="cs-crumb">Inbox</span>' in bar and 'class="inbox-h1"' in bar
    assert 'id="inbox-views"' in bar
    assert "inbox-chrome" not in page and "inbox-toolbar" not in page
    # The suggestion card (when there is one) sits in the empty pane, never above the list.
    empty = page[page.index('class="inbox-pane-empty"') :]
    assert '<div class="inbox-suggest">' in empty[: empty.index("inbox-pane-skeleton")]


def test_the_bar_is_the_same_with_a_conversation_open(shop):
    """Nothing moves when a conversation opens: the bar doesn't change with it."""
    client, account = shop
    chat = _chat(account)
    listed = client.get("/inbox/").content.decode()
    opened = client.get(f"/inbox/{chat.public_id}/").content.decode()

    def bar(html):
        return html[
            html.index('id="inbox-bar"') : html.index(
                "</header>", html.index('id="inbox-bar"')
            )
        ]

    assert bar(listed) == bar(opened)


def test_the_inbox_uses_the_width_and_less_padding(shop):
    client, _ = shop
    page = client.get("/inbox/").content.decode()
    main = page[page.index('<main id="main"') :][:200]
    assert "lg:py-2.5" in main and "max-w-[110rem]" in main


# --- The closed window -----------------------------------------------------------------


def test_whatsapp_closed_window_puts_the_template_picker_in_the_composer(shop):
    client, account = shop
    _template(account, variables=["name"])
    html = _pane(client, _chat(account))

    closed = html[html.index('class="cv-closedwin-body"') :]
    assert 'id="template-inline-list"' in closed[: closed.index('class="cv-composer"')]
    assert 'name="action" value="send_template"' in closed
    # The reply box gives way while the window is closed; the old extra line is gone.
    assert (
        'class="cv-composer" x-ref="composer" @submit.prevent="send()"\n              x-show="windowOpen"'
        in html
    )
    assert "cv-tpl-cta" not in html and "Required</span>" not in html
    # The dialog (window open) uses the same form, under its own ids.
    assert 'id="template-list"' in html


def test_an_account_problem_comes_before_the_templates(shop):
    """A send that failed for billing would fail as a template too: say so, and where to fix it."""
    client, account = shop
    _template(account)
    html = _pane(client, _chat(account))

    problem = html[html.index('class="cv-closedwin-problem"') :]
    problem = problem[: problem.index("data-closed-templates")]
    assert 'x-show="accountProblem && !tplAnyway"' in problem
    assert "Try a template anyway" in problem
    assert 'x-show="!accountProblem || tplAnyway"' in html


def test_no_approved_templates_says_how_to_get_one(shop):
    client, account = shop
    html = _pane(client, _chat(account))

    empty = html[html.index("data-no-templates") :]
    empty = empty[: empty.index("</div>")]
    assert "No approved templates yet" in empty
    assert 'href="/email/templates/?channel=whatsapp"' in empty
    assert 'id="template-inline-list"' not in html


def test_instagram_closed_window_switches_the_reply_box_off(shop):
    client, account = shop
    html = _pane(client, _chat(account, channel="instagram"))

    assert "cv-closedwin-body" not in html  # no templates on Instagram
    assert ':disabled="!windowOpen"' in html
    assert "Reply once they write again" in html
    # Attach and the mic only while a reply can go.
    assert 'x-show="!recording && windowOpen"' in html
    assert 'x-show="canRecord && !draft.trim() && !sending && windowOpen"' in html
    # A failed reply can't be edited and resent until the window opens again.
    assert 'x-show="open && windowOpen && item.m.body && !item.m.unknown"' in html


def test_the_note_explains_why_behind_a_tip(shop):
    client, account = shop
    html = _pane(client, _chat(account))
    note = html[html.index('class="cv-closedwin-note"') :]
    note = note[: note.index("</p>")]
    assert (
        "24-hour window closed" in note
        and "help-tip-label" in note
        and ">Why?" in note.replace("\n", "").replace(" ", "")
    )


# --- The details -----------------------------------------------------------------------


def test_the_details_can_dock_hide_and_resize(shop):
    client, account = shop
    chat = _chat(account, hours_ago=1)
    html = _pane(client, chat)

    assert ":class=\"{ 'is-side-collapsed': sideCollapsed }\"" in html
    assert '@click="toggleInfo()"' in html
    handle = html[html.index('class="cv-side-resize"') :][:400]
    assert 'role="separator"' in handle and "startResize($event)" in handle
    assert "resizeSide(16)" in handle and "resizeSide(-16)" in handle
    # The panel's own close button is for the drawer; docked, the header button hides it.
    assert (
        'class="cv-iconbtn cv-side-close'
        in client.get(
            f"/inbox/{chat.public_id}/panel/", HTTP_HX_REQUEST="true"
        ).content.decode()
    )


def test_the_dock_keeps_the_chat_at_least_420px():
    from django.conf import settings

    css = (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    assert "@container cv (min-width: 44.75rem)" in css
    assert "min(var(--cv-side-w, 320px), calc(100% - 420px - 1rem))" in css
