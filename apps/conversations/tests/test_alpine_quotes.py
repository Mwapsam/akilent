"""Apostrophes in names and messages never break an Alpine expression.

Django escapes ' as &#x27;, which the browser turns back into ' inside an attribute: a
value put in a single-quoted JS string (x-data="{ name: '{{ name }}' }") then ends the
string early, and Alpine logs "Unexpected identifier 's'". Values go through |escapejs,
data-* or json_script instead.

The check renders the inbox and a conversation for a customer, a business and a template
full of apostrophes, decodes each Alpine attribute the way the browser does, and asks a
JS tokenizer-lite whether every single-quoted string closes where it should.
"""

from __future__ import annotations

import datetime
import html
import re

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

pytestmark = pytest.mark.django_db

ALPINE_ATTR = re.compile(
    r"""\s((?:x-[a-z-]+|@[a-z:.\-]+|:[a-z\-]+)(?:\.[a-z\-]+)*)=(["'])(.*?)\2""", re.S
)
QUOTEY = "O'Brien's"


def _unbalanced(expr: str) -> bool:
    """True when a single-quoted string in ``expr`` swallows a stray identifier: an
    apostrophe between letters outside any string, i.e. a string closed early."""
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch in "'\"`":
            j = i + 1
            while j < n and expr[j] != ch:
                j += 2 if expr[j] == "\\" else 1
            if j >= n:
                return True  # never closed
            # A string closed and immediately followed by a letter: 'O'Brien'
            if j + 1 < n and (expr[j + 1].isalpha() or expr[j + 1] == "_"):
                return True
            i = j + 1
            continue
        i += 1
    return False


def _bad_attributes(page: str) -> list[str]:
    bad = []
    for m in ALPINE_ATTR.finditer(page):
        name, raw = m.group(1), m.group(3)
        value = html.unescape(raw)
        if "&#x27;" in raw or "&#39;" in raw:
            bad.append(f"escaped quote in {name}={raw[:160]!r}")
            continue
        if name in (":class",) and "{" not in value and "'" not in value:
            continue
        if _unbalanced(value):
            bad.append(f"{name}={value[:160]!r}")
    return bad


@pytest.fixture(params=["whatsapp", "facebook", "instagram"])
def chat(client, request):
    user = User.objects.create_user(
        "o", "o@example.com", "pw", first_name="D'Arcy", last_name="O'Neil"
    )
    account = Account.objects.create(company_name="O'Brien's Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    contact = Contact.objects.create(
        account=account, first_name="O'Brien's", last_name="Shop", phone="+260971234567"
    )
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel=request.param, is_unread=True
    )
    Message.objects.create(
        account=account,
        conversation=conversation,
        direction="inbound",
        body="O'Brien's here: it's not working, didn't arrive",
        timestamp=timezone.now() - datetime.timedelta(minutes=3),
    )
    from apps.accounts.models import Team
    from apps.conversations.models import FollowUp, SavedReply
    from apps.whatsapp.models import MessageTemplate

    SavedReply.objects.create(
        account=account, title="We're open", body="It's open till 6, don't worry"
    )
    team = Team.objects.create(account=account, name="Sam's team")
    conversation.assigned_team = team
    conversation.save(update_fields=["assigned_team"])
    MessageTemplate.objects.create(
        account=account,
        name="it_s_ready",
        whatsapp_template_name="it_s_ready",
        content="Hi {{1}}, it's ready. Don't forget your receipt.",
        variables=["1"],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    FollowUp.objects.create(
        account=account,
        contact=contact,
        conversation=conversation,
        due_at=timezone.now() + datetime.timedelta(hours=2),
        note="Don't forget: she's waiting",
        source="manual",
    )
    client.force_login(user)
    return client, conversation


def test_inbox_and_conversation_alpine_expressions_survive_apostrophes(chat):
    client, conversation = chat
    pages = {
        "inbox": client.get("/inbox/").content.decode(),
        "detail": client.get(f"/inbox/{conversation.public_id}/").content.decode(),
        "pane": client.get(
            f"/inbox/{conversation.public_id}/",
            headers={"HX-Request": "true", "HX-Target": "conversation-pane-inner"},
        ).content.decode(),
        "panel": client.get(
            f"/inbox/{conversation.public_id}/panel/",
            headers={"HX-Request": "true", "HX-Target": "cv-panel"},
        ).content.decode(),
    }
    problems = {k: _bad_attributes(v) for k, v in pages.items()}
    assert not any(problems.values()), problems
