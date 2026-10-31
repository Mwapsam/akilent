"""Follow-ups outside their page (Stage 4 of docs/design/followups-implementation-checklist.md).

The nav badge, the inbox header link, the chat's composer and side panel, and the inbox
rows all describe the same follow-ups with the same words, and the count everywhere is the
same set of customers as the page's Waiting and Due sections, not just the same number.
"""

import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

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
def signed_in(client, team):
    client.force_login(team[1])
    return client


def _contact(account, n=1, name="Thandi"):
    return Contact.objects.create(
        account=account, phone=f"+26097100000{n}", first_name=name
    )


def _chat(account, contact, channel=Conversation.Channel.WHATSAPP):
    return Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )


def _fu(account, contact, *, minutes, source="manual", conversation=None, to=None):
    return FollowUp.objects.create(
        account=account,
        contact=contact,
        conversation=conversation,
        due_at=timezone.now() + timedelta(minutes=minutes),
        source=source,
        assigned_to=to,
    )


def _badge(client) -> int:
    html = client.get("/inbox/followups/badge/").content.decode()
    match = re.search(r">(\d+)</span>", html)
    return int(match.group(1)) if match else 0


def _page_customers(client, scope):
    resp = client.get(f"/inbox/followups/?list={scope}")
    sections = {s["key"]: s["rows"] for s in resp.context["sections"]}
    return resp, {r["contact"].pk for k in ("waiting", "due") for r in sections[k]}


@pytest.fixture
def mixed(team):
    """Every case the count must get right, for one business."""
    account, me, joseph = team
    a, b, c, d, e, f, g = (_contact(account, n, f"C{n}") for n in range(1, 8))
    _fu(account, a, minutes=-30, conversation=_chat(account, a), to=me)  # mine, due
    _fu(account, b, minutes=-90, conversation=_chat(account, b))  # unassigned, due
    _fu(account, b, minutes=-10, conversation=_chat(account, b), to=me)  # same customer
    _fu(account, c, minutes=-60, conversation=_chat(account, c), to=joseph)  # his
    _fu(account, d, minutes=60, conversation=_chat(account, d), to=me)  # coming up
    _fu(account, e, minutes=-2000, source="missed", conversation=_chat(account, e))
    done = _fu(account, f, minutes=-30, conversation=_chat(account, f), to=me)
    followups.close(done)
    cancelled = _fu(account, g, minutes=-30, conversation=_chat(account, g), to=me)
    followups.close(cancelled, reason=FollowUp.ClosedReason.CANCELLED)
    return account, {"a": a, "b": b, "c": c, "d": d, "e": e}


# ── One count, one set of customers ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_count_is_the_same_customers_as_the_page(signed_in, team, mixed):
    account, people = mixed
    _, me, _ = team

    resp, mine = _page_customers(signed_in, "mine")
    _, everyone = _page_customers(signed_in, "team")

    # Lists, not sets: each customer exactly once (a set would hide a duplicate).
    assert sorted(followups.due_customers(account, user=me)) == sorted(mine)
    assert sorted(followups.due_customers(account)) == sorted(everyone)
    assert mine == {people[k].pk for k in ("a", "b", "e")}
    assert everyone == mine | {people["c"].pk}
    # The badge, the Mine tab and the count are one number from one set.
    assert _badge(signed_in) == followups.due_for(me, account) == 3
    assert resp.context["mine_count"] == 3 and resp.context["team_count"] == 4


@pytest.mark.django_db
def test_the_badge_follows_every_action(signed_in, team, mixed):
    account, people = mixed
    _, me, joseph = team

    def agree():
        _, mine = _page_customers(signed_in, "mine")
        assert _badge(signed_in) == len(mine) == followups.due_for(me, account)
        return len(mine)

    assert agree() == 3
    a = FollowUp.objects.get(contact=people["a"])
    signed_in.post(f"/inbox/followups/{a.pk}/snooze/", {"when": "tomorrow"})
    assert agree() == 2

    missed = FollowUp.objects.get(contact=people["e"])
    signed_in.post(f"/inbox/followups/{missed.pk}/complete/")
    assert agree() == 1

    # One of two follow-ups for B is cancelled: B still has work, so still counts once.
    first_b = FollowUp.objects.filter(contact=people["b"]).order_by("due_at").first()
    signed_in.post(f"/inbox/followups/{first_b.pk}/cancel/")
    assert agree() == 1

    last_b = FollowUp.objects.get(contact=people["b"], done_at__isnull=True)
    signed_in.post(f"/inbox/followups/{last_b.pk}/reassign/", {"user": joseph.pk})
    assert agree() == 0
    # Given away, not gone: it is still the whole team's work.
    assert followups.due_for_team(account) == 2


@pytest.mark.django_db
def test_the_badge_says_whose_count_it_is(signed_in, team, mixed):
    resp = signed_in.get("/inbox/followups/badge/?toast=1")
    html = resp.content.decode()
    assert 'aria-label="3 follow-ups due for you"' in html
    assert "3%20follow-ups%20due%20for%20you" in resp["X-Toast"]


@pytest.mark.django_db
def test_nothing_due_shows_no_badge(signed_in, team):
    account, me, _ = team
    thandi = _contact(account)
    _fu(account, thandi, minutes=60, conversation=_chat(account, thandi), to=me)
    assert signed_in.get("/inbox/followups/badge/").content == b""


# ── The composer ─────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_composer_offers_a_follow_up_for_the_team_only(signed_in, team):
    account, _, _ = team
    chat = _chat(account, _contact(account))
    html = signed_in.get(f"/inbox/{chat.public_id}/").content.decode()
    assert copy.FOLLOW_UP_TOMORROW in html
    assert copy.INTERNAL_ONLY_SHORT in html
    assert "Remind" not in html and "Reminder" not in html


@pytest.mark.django_db
def test_once_set_the_composer_shows_the_pages_words_and_change(signed_in, team):
    account, me, _ = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    signed_in.post(
        f"/inbox/{chat.public_id}/", {"action": "create_followup", "when": "tomorrow"}
    )
    html = signed_in.get(f"/inbox/{chat.public_id}/").content.decode()
    page = signed_in.get("/inbox/followups/?list=mine")
    sections = {s["key"]: s["rows"] for s in page.context["sections"]}
    row = sections["coming_up"][0]

    composer = re.search(r"data-composer-followup>(.*?)</span>", html, re.S).group(1)
    assert f"Follow-up {row['main']['when']}" in composer  # same "tomorrow 09:00"
    assert "data-composer-change" in html and "getElementById('cv-followup')" in html
    assert 'id="cv-followup"' in html
    assert copy.FOLLOW_UP_TOMORROW not in html
    assert "Reminder" not in html


@pytest.mark.django_db
def test_a_waiting_customer_reads_as_waiting_not_as_a_reminder(signed_in, team):
    account, _, _ = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    _fu(account, thandi, minutes=-60 * 30, source="missed", conversation=chat)
    html = signed_in.get(f"/inbox/{chat.public_id}/").content.decode()
    composer = re.search(r"data-composer-followup>(.*?)</span>", html, re.S).group(1)
    assert "Waiting for your reply · for " in composer


# ── The side panel ───────────────────────────────────────────────────────────────────────


def _panel(html):
    start = html.index("data-panel-followup")
    return html[start : html.index("Internal notes", start)]


@pytest.mark.django_db
def test_the_panel_describes_the_follow_up_as_the_page_does(signed_in, team):
    account, me, _ = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    f = _fu(account, thandi, minutes=-180, conversation=chat, to=me)
    f.note = "Send the blue dress photos"
    f.save()
    _fu(account, thandi, minutes=600, conversation=_chat(account, thandi, "instagram"))

    panel = _panel(signed_in.get(f"/inbox/{chat.public_id}/").content.decode())
    assert copy.YOU_SET_THIS in panel and "3h late" in panel
    assert "Send the blue dress photos" in panel and "for you" in panel
    assert f"/inbox/followups/{f.pk}/complete/" in panel
    assert f"/inbox/followups/{f.pk}/snooze/" in panel
    assert f"/inbox/followups/{f.pk}/cancel/" in panel
    assert copy.NOT_NEEDED_CONFIRM in panel
    assert ">Pick a date<" in panel and "Next week" in panel
    assert "+1 more for this customer" in panel
    assert f"contact={thandi.public_id}" in panel
    assert "All follow-ups for Thandi" in panel
    assert "Snooze" not in panel and "list" not in _text(panel)


@pytest.mark.django_db
def test_the_panel_hides_the_missed_chat_note(signed_in, team):
    account, _, _ = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    f = _fu(account, thandi, minutes=-60 * 30, source="missed", conversation=chat)
    f.note = "No reply sent - customer has been waiting"
    f.save()
    panel = _panel(signed_in.get(f"/inbox/{chat.public_id}/").content.decode())
    assert copy.WAITING_LABEL in panel
    assert "No reply sent" not in panel


@pytest.mark.django_db
def test_the_panel_sets_a_follow_up_with_the_promise(signed_in, team):
    account, _, _ = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    panel = _panel(signed_in.get(f"/inbox/{chat.public_id}/").content.decode())
    assert copy.SET_FOLLOWUP in panel and copy.INTERNAL_ONLY in panel
    assert 'value="next_week"' in panel and 'value="custom"' in panel

    resp = signed_in.post(
        f"/inbox/{chat.public_id}/",
        {"action": "create_followup", "when": "next_week", "note": "Quote"},
    )
    assert resp.status_code == 302
    f = FollowUp.objects.get(conversation=chat)
    assert f.note == "Quote" and f.due_at > timezone.now() + timedelta(days=6)


@pytest.mark.django_db
def test_not_needed_from_the_panel_keeps_the_history(signed_in, team):
    account, me, _ = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    f = _fu(account, thandi, minutes=-5, conversation=chat, to=me)
    resp = signed_in.post(
        f"/inbox/followups/{f.pk}/cancel/",
        {"next": f"/inbox/{chat.public_id}/"},
        follow=True,
    )
    f.refresh_from_db()
    assert f.closed_reason == FollowUp.ClosedReason.CANCELLED
    assert [str(m) for m in resp.context["messages"]] == [copy.CANCELLED]
    assert copy.FOLLOW_UP_TOMORROW in resp.content.decode()  # offered again


# ── The inbox ────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_reminders_tab_is_gone_and_its_link_lands_on_follow_ups(signed_in, team):
    html = signed_in.get("/inbox/").content.decode()
    assert "view=followups" not in html and ">Reminders<" not in _text(html)
    resp = signed_in.get("/inbox/?view=followups")
    assert resp.status_code == 302 and resp["Location"] == "/inbox/followups/"


@pytest.mark.django_db
def test_the_inbox_header_link_uses_the_badge_count(signed_in, team):
    html = signed_in.get("/inbox/").content.decode()
    link = html[html.index("data-inbox-followups-link") :]
    link = link[: link.index("</a>")]
    assert 'hx-get="/inbox/followups/badge/"' in link
    assert "Follow-ups due" not in html


@pytest.mark.django_db
def test_inbox_rows_say_follow_up_and_when_without_doubling_missed(signed_in, team):
    account, me, _ = team
    set_one, waiting = _contact(account, 1, "Set"), _contact(account, 2, "Wait")
    _fu(account, set_one, minutes=60 * 24, conversation=_chat(account, set_one), to=me)
    _fu(
        account,
        waiting,
        minutes=-60 * 30,
        source="missed",
        conversation=_chat(account, waiting),
    )
    html = signed_in.get("/inbox/?view=all").content.decode()
    tags = re.findall(r"data-inbox-followup>.*?</svg>\s*([^<]+)</span>", html, re.S)
    assert [t.strip() for t in tags] == ["Follow-up tomorrow"]


def _text(html):
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
