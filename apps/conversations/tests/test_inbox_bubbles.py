"""Inbox Phase 5B and 5C: one look for bubbles, errors said once, the template picker.

- 5B: every channel has the same bubbles (test_inbox_confidence); an account-level failure
  (billing, display name, connection) is said once, in a banner above the composer with its
  fix, and each message it stopped says only "Not sent"; "Sent by AI" is a chip.
- 5C: the closed-window picker is one button that opens over the thread; templates read by
  their label, can be searched, ask for each value by a label with a live preview, can be
  cancelled, and Send waits until everything is in.

Grouping, the hover time, the popover's position and the preview as you type are checked in
the browser by tools/qa/inbox_qa.mjs (QA_PICKER).
"""

import datetime

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.whatsapp.models import MessageTemplate

pytestmark = pytest.mark.django_db

PANE = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "conversation-pane-inner"}


@pytest.fixture
def shop(client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _chat(account, *, hours_ago=30):
    contact = Contact.objects.create(
        account=account, first_name="Thandi", phone="+260971234567"
    )
    chat = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    Message.objects.create(
        account=account,
        conversation=chat,
        direction="inbound",
        body="Hi",
        timestamp=timezone.now() - datetime.timedelta(hours=hours_ago),
    )
    return chat


def _template(account, name="download_app", content="Hi {{1}}, get the app.", **kw):
    return MessageTemplate.objects.create(
        account=account,
        name=name,
        language_code="en",
        content=content,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        **kw,
    )


def _pane(client, chat):
    return client.get(f"/inbox/{chat.public_id}/", **PANE).content.decode()


# --- Labels ----------------------------------------------------------------------------


def test_a_template_reads_by_its_label():
    t = MessageTemplate(name="download_app")
    assert t.label == "Download app"
    assert MessageTemplate(name="order-ready__now").label == "Order ready now"


def test_a_numbered_value_is_asked_for_by_a_label_with_metas_example():
    t = MessageTemplate(variables=["1", "first_name"], variable_examples=["Thandi"])
    assert t.fields() == [
        {"name": "1", "label": "Value 1", "example": "Thandi"},
        {"name": "first_name", "label": "First name", "example": ""},
    ]


# --- The picker ------------------------------------------------------------------------


def test_the_closed_window_picker_opens_over_the_thread(shop):
    client, account = shop
    t = _template(account, variables=["1"], variable_examples=["Thandi"])
    html = _pane(client, _chat(account))

    pick = html[html.index('class="cv-tplpick"') :]
    pick = pick[: pick.index("</form>")]
    assert ">Send a template</button>" in pick
    assert 'class="cv-tplpop"' in pick and 'x-show="pick"' in pick
    assert (
        ">Download app<" in pick and "download_app" not in pick.split("data-search")[0]
    )
    assert f'data-tpl-option="{t.pk}"' in pick and 'data-fields="1"' in pick
    assert "Value 1" in pick and 'placeholder="Thandi"' in pick
    assert 'x-text="preview()"' in pick
    assert ">Cancel</button>" in pick
    assert ':disabled="!ready()">Send template' in pick


def test_search_shows_once_there_are_more_than_a_few(shop):
    client, account = shop
    for i in range(3):
        _template(account, name=f"t_{i}")
    chat = _chat(account)
    assert 'id="template-inline-search"' not in _pane(client, chat)
    _template(account, name="t_3")
    assert 'id="template-inline-search"' in _pane(client, chat)


# --- Errors said once ------------------------------------------------------------------


def test_an_account_problem_is_one_banner_and_each_message_says_not_sent(shop):
    client, account = shop
    _template(account)
    html = _pane(client, _chat(account))

    assert html.count("data-account-banner") == 1
    assert html.count("accountProblem.failureReason") == 1
    # The bubble's line: "Not sent" and a tap for why, its fix only in the banner.
    assert (
        "<span>Not sent</span>" in html and 'aria-label="Why it wasn\'t sent"' in html
    )
    assert "item.m.failureFix.label" not in html


def test_sent_by_ai_is_a_chip(shop):
    client, account = shop
    html = _pane(client, _chat(account, hours_ago=1))
    assert 'class="cv-ai-chip" title="Sent by AI">AI</span>' in html
