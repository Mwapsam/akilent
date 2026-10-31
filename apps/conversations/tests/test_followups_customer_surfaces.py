"""Follow-ups on the contact page, the lead page, the dashboard and Business Health
(Stage 5 of docs/design/followups-implementation-checklist.md).

Every number is one of the two shared counts: ``due_for`` (Mine: the nav badge) or
``due_for_team`` (Whole team: the dashboard tile and Business Health, which were already
business-wide and stay so). Every follow-up reads as its row on the Follow-ups page, and
every action leads to the right customer or chat.
"""

import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import followup_copy as copy
from apps.conversations import followups, reporting
from apps.conversations.models import Conversation, FollowUp
from apps.crm.models import Lead


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


def _contact(account, n, name):
    return Contact.objects.create(
        account=account, phone=f"+26097200000{n}", first_name=name
    )


def _chat(account, contact):
    return Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP
    )


def _fu(account, contact, *, minutes, chat=None, to=None, source="manual", note=""):
    return FollowUp.objects.create(
        account=account,
        contact=contact,
        conversation=chat,
        due_at=timezone.now() + timedelta(minutes=minutes),
        source=source,
        assigned_to=to,
        note=note,
    )


@pytest.fixture
def world(team):
    """Every state the counts and cards must get right."""
    account, me, joseph = team
    people = {}
    for n, name in enumerate(
        ("Amos", "Bwalya", "Chola", "Dalitso", "Esther", "Faith"), 1
    ):
        people[name] = _contact(account, n, name)
    a, b, c, d, e, f = (people[k] for k in people)
    chat_b = _chat(account, b)
    _fu(account, a, minutes=-30, chat=_chat(account, a), to=me)  # mine, overdue
    _fu(account, b, minutes=-90, chat=chat_b, note="Send the quote")  # unassigned
    _fu(account, b, minutes=60 * 24, chat=_chat(account, b), to=me)  # same customer
    _fu(account, c, minutes=-60, chat=_chat(account, c), to=joseph)  # teammate's
    _fu(account, d, minutes=120, chat=_chat(account, d), to=me)  # coming up
    _fu(account, e, minutes=-45, to=me)  # no chat at all
    done = _fu(account, f, minutes=-30, chat=_chat(account, f), to=me)
    followups.close(done)
    cancelled = _fu(account, f, minutes=-20, chat=_chat(account, f), to=me)
    followups.close(cancelled, reason=FollowUp.ClosedReason.CANCELLED)
    return account, people


def _badge(client) -> int:
    m = re.search(
        r">(\d+)</span>", client.get("/inbox/followups/badge/").content.decode()
    )
    return int(m.group(1)) if m else 0


def _tab_counts(client):
    resp = client.get("/inbox/followups/?list=team")
    return resp.context["mine_count"], resp.context["team_count"]


def _tile(client) -> int:
    html = client.get("/dashboard/work-queue/").content.decode()
    tile = html[html.index('data-tile="followups"') :]
    return int(re.search(r'work-tile-value">(\d+)<', tile).group(1))


# ── One definition, two scopes ──────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_mine_and_whole_team_counts_agree_on_every_surface(signed_in, team, world):
    account, _ = world
    _, me, _ = team
    mine, everyone = _tab_counts(signed_in)
    # Mine: Amos, Bwalya (unassigned), Esther (no chat). Team adds Chola (Joseph's).
    # Dalitso is coming up; Faith's are done and cancelled.
    assert mine == followups.due_for(me, account) == _badge(signed_in) == 3
    assert everyone == followups.due_for_team(account) == 4
    assert _tile(signed_in) == everyone
    assert reporting.at_risk(account)["counts"]["followups_due"] == everyone


@pytest.mark.django_db
def test_the_dashboard_tile_says_whose_count_and_opens_that_tab(signed_in, world):
    html = signed_in.get("/dashboard/work-queue/").content.decode()
    tile = html[html.index('href="/inbox/followups/?list=team"') :]
    tile = tile[: tile.index("</a>")]
    assert "whole team" in tile and 'data-tile="followups"' in tile


@pytest.mark.django_db
def test_business_health_says_whole_team_and_opens_that_tab(signed_in, world):
    account, _ = world
    action = next(
        a for a in reporting.at_risk(account)["actions"] if a["key"] == "followups_due"
    )
    assert action["text"] == "4 follow-ups due for the whole team"
    assert action["url"] == "/inbox/followups/?list=team"
    assert (
        "4 follow-ups due for the whole team"
        in signed_in.get("/insights/").content.decode()
    )


@pytest.mark.django_db
def test_actions_move_every_count_together(signed_in, team, world):
    account, people = world
    _, me, _ = team

    def counts():
        mine, everyone = _tab_counts(signed_in)
        assert _badge(signed_in) == mine and _tile(signed_in) == everyone
        assert reporting.at_risk(account)["counts"]["followups_due"] == everyone
        return mine, everyone

    assert counts() == (3, 4)
    contact_page = f"/contacts/{people['Chola'].public_id}/"
    chola = FollowUp.objects.get(contact=people["Chola"])
    signed_in.post(f"/inbox/followups/{chola.pk}/complete/", {"next": contact_page})
    assert counts() == (3, 3)  # Joseph's: the team's work, not mine

    amos = FollowUp.objects.get(contact=people["Amos"])
    signed_in.post(f"/inbox/followups/{amos.pk}/snooze/", {"when": "tomorrow"})
    assert counts() == (2, 2)

    # One of Bwalya's two is cancelled; the other is tomorrow, so Bwalya leaves both.
    first = FollowUp.objects.filter(contact=people["Bwalya"]).order_by("due_at")[0]
    signed_in.post(f"/inbox/followups/{first.pk}/cancel/")
    first.refresh_from_db()
    assert first.closed_reason == FollowUp.ClosedReason.CANCELLED
    assert counts() == (1, 1)


# ── The contact page ────────────────────────────────────────────────────────────────────


def _card(html):
    start = html.index("data-customer-followups")
    return html[start : html.index("</section>", start)]


@pytest.mark.django_db
def test_the_contact_card_reads_as_the_page_and_hides_nothing(signed_in, team, world):
    account, people = world
    bwalya = people["Bwalya"]
    card = _card(signed_in.get(f"/contacts/{bwalya.public_id}/").content.decode())
    row = next(
        r
        for s in signed_in.get("/inbox/followups/?list=team").context["sections"]
        for r in s["rows"]
        if r["contact"].pk == bwalya.pk
    )
    # Both follow-ups, the most urgent first, in the page's own words.
    assert card.count("data-customer-followup=") == 2
    assert card.index(row["main"]["when"]) < card.index(row["others"][0]["when"])
    assert "for the team" in card and "for you" in card and "Send the quote" in card
    assert copy.FROM_AUTOMATION not in card and copy.YOU_SET_THIS in card
    for f in FollowUp.objects.filter(contact=bwalya):
        assert f"/inbox/followups/{f.pk}/complete/" in card
        assert f"/inbox/followups/{f.pk}/snooze/" in card
        assert f"/inbox/followups/{f.pk}/cancel/" in card
        assert f"/inbox/{f.conversation.public_id}/" in card  # opens its own chat
    assert f"?list=team&amp;contact={bwalya.public_id}" in card
    assert copy.all_for("Bwalya") in card and copy.INTERNAL_ONLY in card


@pytest.mark.django_db
def test_done_and_cancelled_never_show_on_the_card(signed_in, world):
    _, people = world
    card = _card(
        signed_in.get(f"/contacts/{people['Faith'].public_id}/").content.decode()
    )
    assert "data-customer-followup=" not in card
    assert copy.no_followups_for("Faith") in card
    assert "Reminder" not in card and "Remind me" not in card


@pytest.mark.django_db
def test_a_customer_without_a_chat_is_understandable(signed_in, team, world):
    account, people = world
    esther = people["Esther"]
    card = _card(signed_in.get(f"/contacts/{esther.public_id}/").content.decode())
    assert "no chat yet: opens their customer page" in card
    assert f'href="/contacts/{esther.public_id}/"' in card
    assert 'name="conversation"' not in card and copy.NO_CHAT_NOTE in card

    resp = signed_in.post(
        "/inbox/followups/new/",
        {"contact": esther.public_id, "when": "next_week", "next": "/x/"},
    )
    assert resp.status_code == 302
    new = FollowUp.objects.filter(contact=esther).latest("pk")
    assert new.conversation is None and new.created_by == team[1]
    page = signed_in.get(f"/inbox/followups/?list=team&contact={esther.public_id}")
    assert "Open customer" in page.content.decode()


@pytest.mark.django_db
def test_a_new_follow_up_from_the_contact_page_belongs_to_their_latest_chat(
    signed_in, world
):
    account, people = world
    dalitso = people["Dalitso"]
    chat = Conversation.objects.get(contact=dalitso)
    card = _card(signed_in.get(f"/contacts/{dalitso.public_id}/").content.decode())
    assert f'name="conversation" value="{chat.public_id}"' in card
    resp = signed_in.post(
        "/inbox/followups/new/",
        {
            "contact": dalitso.public_id,
            "conversation": chat.public_id,
            "when": "1h",
            "next": f"/contacts/{dalitso.public_id}/",
        },
        follow=True,
    )
    # Dalitso already had one on that chat: kept, earlier time wins, and it says so.
    messages = [str(m) for m in resp.context["messages"]]
    assert messages and messages[0].startswith("This chat already has a follow-up on")
    assert FollowUp.objects.filter(contact=dalitso, done_at__isnull=True).count() == 1


@pytest.mark.django_db
def test_setting_a_follow_up_refuses_someone_elses_customer_or_chat(signed_in, world):
    _, people = world
    other = Account.objects.create(company_name="Elsewhere")
    stranger = _contact(other, 9, "Stranger")
    assert (
        signed_in.post(
            "/inbox/followups/new/", {"contact": stranger.public_id, "when": "1h"}
        ).status_code
        == 404
    )
    amos_chat = Conversation.objects.get(contact=people["Amos"])
    resp = signed_in.post(
        "/inbox/followups/new/",
        {
            "contact": people["Dalitso"].public_id,
            "conversation": amos_chat.public_id,
            "when": "1h",
        },
    )
    assert resp.status_code == 404


# ── The lead page ───────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_lead_page_sets_a_follow_up_on_the_chat_the_interest_came_from(
    signed_in, team, world
):
    account, people = world
    amos = people["Amos"]
    chat = Conversation.objects.get(contact=amos)
    lead = Lead.objects.create(account=account, contact=amos, conversation=chat)
    html = signed_in.get(f"/sales/leads/{lead.public_id}/").content.decode()
    card = _card(html)
    assert copy.SET_FOLLOWUP in card and copy.INTERNAL_ONLY in card
    assert f'name="conversation" value="{chat.public_id}"' in card
    assert "Reminder" not in html

    FollowUp.objects.filter(contact=amos).update(done_at=timezone.now())
    resp = signed_in.post(
        "/inbox/followups/new/",
        {
            "contact": amos.public_id,
            "conversation": chat.public_id,
            "when": "tomorrow",
            "note": "Bring the catalogue",
            "next": f"/sales/leads/{lead.public_id}/",
        },
        follow=True,
    )
    new = FollowUp.objects.get(contact=amos, done_at__isnull=True)
    assert new.conversation == chat and new.note == "Bring the catalogue"
    message = next(str(m) for m in resp.context["messages"])
    assert message.startswith("Follow-up set for")
    assert "Bring the catalogue" in _card(resp.content.decode())


# ── Business Health's insight button ────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_waiting_insight_button_does_not_claim_to_create_follow_ups(team):
    from unittest import mock

    from apps.insights import rules

    waiting = mock.MagicMock()
    waiting.filter.return_value.distinct.return_value.count.return_value = 2
    with (
        mock.patch("apps.conversations.state.needs_attention", return_value=waiting),
        mock.patch(
            "apps.conversations.state.average_first_response_seconds",
            return_value=None,
        ),
    ):
        insight = rules.rule_lead_followup_gap(team[0])
    assert insight.suggested_action["label"] == "See 2 waiting conversations"
    assert insight.suggested_action["action"] == "create_followup"  # same destination
