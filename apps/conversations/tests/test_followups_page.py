"""The Follow-ups page (Stage 3 of docs/design/followups-implementation-checklist.md).

Read as a business owner would: who is on the page, why, what the main button does and
which chat it concerns. Behaviour and words: docs/design/followups-interaction-contract.md.
"""

import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.html import escape

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import followup_copy as copy
from apps.conversations import followups
from apps.conversations.models import Conversation, FollowUp


@pytest.fixture
def team(db):
    account = Account.objects.create(company_name="Mwamba Kitchen")
    me = User.objects.create_user("me", "me@example.com", "pw", first_name="Ada")
    joseph = User.objects.create_user("jo", "jo@example.com", "pw", first_name="Joseph")
    for user, role in ((me, Membership.Role.OWNER), (joseph, Membership.Role.MEMBER)):
        Membership.objects.create(user=user, account=account, role=role)
    return account, me, joseph


@pytest.fixture
def page(client, team):
    _, me, _ = team
    client.force_login(me)

    def get(query=""):
        resp = client.get(f"/inbox/followups/{query}")
        assert resp.status_code == 200
        return resp

    return get


def _contact(account, phone="+260971000001", name="Thandi"):
    return Contact.objects.create(account=account, phone=phone, first_name=name)


def _chat(account, contact, channel=Conversation.Channel.WHATSAPP):
    return Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )


def _fu(
    account, contact, *, minutes, source="manual", conversation=None, to=None, note=""
):
    return FollowUp.objects.create(
        account=account,
        contact=contact,
        conversation=conversation,
        due_at=timezone.now() + timedelta(minutes=minutes),
        source=source,
        assigned_to=to,
        note=note,
    )


def _sections(resp):
    return {s["key"]: s["rows"] for s in resp.context["sections"]}


def _section_html(html, key):
    start = html.index(f'data-section="{key}"')
    end = html.find("data-section=", start + 1)
    return html[start : end if end != -1 else len(html)]


def _visible(html):
    body = html[html.index("<h1") : html.index("Email me when")]
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


# ── Who is on the page, and where ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_one_row_per_customer_across_chats_led_by_the_most_urgent(team, page):
    account, me, _ = team
    thandi = _contact(account)
    wa, ig = (
        _chat(account, thandi),
        _chat(account, thandi, Conversation.Channel.INSTAGRAM),
    )
    _fu(
        account,
        thandi,
        minutes=-120,
        conversation=wa,
        to=me,
        note="Send blue dress photos",
    )
    _fu(account, thandi, minutes=60 * 24, conversation=ig, to=me)

    resp = page()
    html = resp.content.decode()
    due = _section_html(html, "due")

    assert len(_sections(resp)["due"]) == 1
    assert due.count("data-followup-row=") == 1
    assert "Thandi" in due and "“Send blue dress photos”" in due
    assert "2h late" in due and "WhatsApp chat" in due and "for you" in due
    # The other chat stays one tap away, with which chat and when.
    others = due[due.index("data-followup-others") :]
    assert f'href="/inbox/{ig.public_id}/"' in others
    assert "Instagram chat" in others and "tomorrow" in others
    assert 'data-section="coming_up"' in html and _sections(resp)["coming_up"] == []


@pytest.mark.django_db
def test_a_missed_chat_appears_once_waiting_with_reply_now(team, page):
    account, me, _ = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    _fu(account, thandi, minutes=-30, conversation=chat, to=me)
    _fu(
        account,
        thandi,
        minutes=-5,
        conversation=chat,
        source="missed",
        note="No reply sent - customer has been waiting",
    )

    html = page().content.decode()
    waiting = _section_html(html, "waiting")

    assert html.count("data-followup-row=") == 1
    assert copy.WAITING_LABEL in waiting and ">for " in waiting
    assert "No reply sent" not in waiting  # the job's own note only repeats the label
    main = re.search(r'<a href="([^"]+)"[^>]*data-main-action>([^<]+)<', waiting)
    assert main.group(1) == f"/inbox/{chat.public_id}/?reply=1"
    assert main.group(2).strip() == copy.REPLY_NOW


@pytest.mark.django_db
def test_due_and_coming_up_open_the_chat_and_never_complete(team, page):
    account, me, _ = team
    soon = _contact(account)
    chat = _chat(account, soon)
    item = _fu(account, soon, minutes=60 * 30, conversation=chat, to=me)

    html = page().content.decode()
    coming = _section_html(html, "coming_up")
    main = re.search(r'<a href="([^"]+)"[^>]*data-main-action>([^<]+)<', coming)

    assert main.group(1) == f"/inbox/{chat.public_id}/"
    assert main.group(2).strip() == copy.OPEN_CHAT
    # Opening it is a plain link: nothing is closed by looking.
    page()
    assert FollowUp.objects.get(pk=item.pk).done_at is None


@pytest.mark.django_db
def test_a_customer_without_a_chat_opens_their_customer_page(team, page):
    account, me, _ = team
    grace = _contact(account, "+260971000002", "Grace")
    _fu(account, grace, minutes=-10, to=me)

    due = _section_html(page().content.decode(), "due")
    main = re.search(r'<a href="([^"]+)"[^>]*data-main-action>([^<]+)<', due)

    assert main.group(1) == f"/contacts/{grace.public_id}/"
    assert main.group(2).strip() == "Open customer"
    assert "no chat yet: opens their customer page" in due


@pytest.mark.django_db
def test_each_section_explains_itself_when_empty(page):
    html = page().content.decode()

    for key in ("waiting", "due", "coming_up"):
        assert f'data-empty="{key}"' in html
        assert escape(copy.EMPTY_STATES[key]) in _section_html(html, key)


@pytest.mark.django_db
def test_the_promise_is_on_the_page(page):
    assert copy.INTERNAL_ONLY in page().content.decode()


# ── Mine, whole team, one customer ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_mine_and_whole_team(team, page):
    account, me, joseph = team
    _fu(account, _contact(account), minutes=-10, to=me)
    _fu(account, _contact(account, "+260971000002", "Grace"), minutes=-10, to=joseph)

    mine, everyone = page("?list=mine"), page("?list=team")

    assert [r["contact"].first_name for r in _sections(mine)["due"]] == ["Thandi"]
    assert {r["contact"].first_name for r in _sections(everyone)["due"]} == {
        "Thandi",
        "Grace",
    }
    assert "for Joseph" in everyone.content.decode()
    # The tab counts are the shared counts.
    assert mine.context["mine_count"] == followups.due_for(me, account) == 1
    assert mine.context["team_count"] == followups.due_for_team(account) == 2


@pytest.mark.django_db
def test_contact_filter_shows_one_customer_and_a_way_back(team, page):
    account, me, _ = team
    thandi, grace = _contact(account), _contact(account, "+260971000002", "Grace")
    _fu(account, thandi, minutes=-10, to=me)
    _fu(account, grace, minutes=-10, to=me)

    resp = page(f"?contact={grace.public_id}")
    html = resp.content.decode()

    assert [r["contact"] for r in _sections(resp)["due"]] == [grace]
    assert "data-followup-filter" in html and "Show everyone" in html


@pytest.mark.django_db
def test_contact_filter_refuses_another_business(client, team):
    account, me, _ = team
    other = Account.objects.create(company_name="Elsewhere")
    stranger = _contact(other)
    client.force_login(me)

    assert (
        client.get(f"/inbox/followups/?contact={stranger.public_id}").status_code == 404
    )


# ── Actions on the row ───────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_row_actions_post_to_the_leading_followup(team, page):
    account, me, _ = team
    thandi = _contact(account)
    lead = _fu(account, thandi, minutes=-10, conversation=_chat(account, thandi), to=me)

    row = _section_html(page().content.decode(), "due")

    assert f'action="/inbox/followups/{lead.pk}/complete/"' in row
    assert f'action="/inbox/followups/{lead.pk}/snooze/"' in row
    for value in ("1h", "tomorrow", "next_week"):
        assert f'name="when" value="{value}"' in row
    assert "Pick a date" in row and 'name="custom_due_at"' in row
    assert f'action="/inbox/followups/{lead.pk}/reassign/"' in row
    assert f'action="/inbox/followups/{lead.pk}/edit/"' in row
    assert f'action="/inbox/followups/{lead.pk}/cancel/"' in row
    assert copy.NOT_NEEDED_CONFIRM in row


@pytest.mark.django_db
def test_acting_from_the_page_comes_back_to_it_with_what_happened(client, team):
    account, me, _ = team
    thandi = _contact(account)
    item = _fu(account, thandi, minutes=-10, to=me)
    client.force_login(me)

    resp = client.post(
        f"/inbox/followups/{item.pk}/complete/",
        {"next": "/inbox/followups/?list=mine"},
        follow=True,
    )

    assert resp.redirect_chain[-1][0] == "/inbox/followups/?list=mine"
    assert copy.MARKED_DONE in [str(m) for m in resp.context["messages"]]
    assert _sections(resp)["due"] == []


# ── Words ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_page_uses_only_the_agreed_words(team, page):
    account, me, joseph = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    _fu(
        account,
        thandi,
        minutes=-5,
        conversation=chat,
        source="missed",
        note="No reply sent - customer has been waiting",
    )
    _fu(
        account,
        _contact(account, "+260971000002", "Grace"),
        minutes=-5,
        source="policy",
        to=me,
    )
    _fu(
        account,
        _contact(account, "+260971000003", "Mary"),
        minutes=90,
        source="workflow",
        to=joseph,
    )

    text = _visible(page("?list=team").content.decode())

    for banned in (
        "Reminder",
        "reminder",
        "Snooze",
        "snooze",
        "Remove",
        "From Insights",
        "policy",
        "Insights rule",
        "'s list",
        "Move",
    ):
        assert banned not in text, banned
    assert copy.FROM_AUTOMATION in text and copy.WAITING_LABEL in text


@pytest.mark.django_db
def test_reply_now_opens_the_chat_with_the_reply_box_ready(team, page, client):
    account, me, _ = team
    chat = _chat(account, _contact(account))

    ready = client.get(f"/inbox/{chat.public_id}/?reply=1").content.decode()
    plain = client.get(f"/inbox/{chat.public_id}/").content.decode()

    assert 'x-init="setTimeout(() => $el.focus(), 50)"' in ready
    assert "$el.focus(), 50" not in plain
