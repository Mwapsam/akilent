"""Follow-ups: the counting, grouping, state and wording rules of the interaction contract
(docs/design/followups-interaction-contract.md), Stage 2."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import followup_copy, followups
from apps.conversations.models import Conversation, FollowUp

NOW = timezone.now()


@pytest.fixture
def team(db):
    account = Account.objects.create(company_name="Mwamba Kitchen")
    me = User.objects.create_user("me", "me@example.com", "pw", first_name="Ada")
    joseph = User.objects.create_user("jo", "jo@example.com", "pw", first_name="Joseph")
    for user, role in ((me, Membership.Role.OWNER), (joseph, Membership.Role.MEMBER)):
        Membership.objects.create(user=user, account=account, role=role)
    return account, me, joseph


def _contact(account, phone="+260971000001", name="Thandi"):
    return Contact.objects.create(account=account, phone=phone, first_name=name)


def _chat(account, contact, channel=Conversation.Channel.WHATSAPP):
    return Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )


def _fu(account, contact, *, due, source="manual", conversation=None, to=None, by=None):
    return FollowUp.objects.create(
        account=account,
        contact=contact,
        conversation=conversation,
        due_at=due,
        source=source,
        assigned_to=to,
        created_by=by,
    )


# ── Grouping: one row per customer ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_one_customer_with_several_chats_is_one_row_led_by_the_most_urgent(team):
    account, me, _ = team
    thandi = _contact(account)
    wa = _chat(account, thandi)
    ig = _chat(account, thandi, Conversation.Channel.INSTAGRAM)
    later = _fu(account, thandi, due=NOW + timedelta(days=1), conversation=ig, to=me)
    late = _fu(account, thandi, due=NOW - timedelta(hours=2), conversation=wa, to=me)

    sections = followups.rows_for(me, account, now=NOW)

    assert [len(sections[s]) for s in followups.SECTION_ORDER] == [0, 1, 0]
    row = sections["due"][0]
    assert row.primary == late
    assert row.others == [later]
    assert row.main_action == "open"
    assert row.target_url == f"/inbox/{wa.public_id}/"


@pytest.mark.django_db
def test_missed_and_manual_together_are_one_row_waiting_for_a_reply(team):
    account, me, _ = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    _fu(account, thandi, due=NOW - timedelta(hours=1), conversation=chat, to=me)
    missed = _fu(
        account,
        thandi,
        due=NOW - timedelta(minutes=5),
        conversation=chat,
        source="missed",
    )

    sections = followups.rows_for(me, account, now=NOW)

    assert len(sections["waiting"]) == 1 and not sections["due"]
    row = sections["waiting"][0]
    assert row.primary == missed
    assert row.main_action == "reply"
    assert row.target_url.endswith("?reply=1")


@pytest.mark.django_db
def test_mine_qualifies_a_customer_by_one_item_but_shows_all_with_owners(team):
    account, me, joseph = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    joseph_item = _fu(
        account, thandi, due=NOW - timedelta(hours=3), conversation=chat, to=joseph
    )
    unassigned = _fu(
        account,
        thandi,
        due=NOW + timedelta(hours=5),
        conversation=_chat(account, thandi, Conversation.Channel.FACEBOOK),
    )
    only_joseph = _contact(account, "+260971000002", "Grace")
    _fu(account, only_joseph, due=NOW - timedelta(hours=1), to=joseph)

    mine = followups.rows_for(me, account, scope="mine", now=NOW)
    rows = [r for s in followups.SECTION_ORDER for r in mine[s]]

    assert [r.contact for r in rows] == [thandi]  # Grace is only Joseph's
    row = rows[0]
    # My qualifying item (the unassigned one) leads; Joseph's stays visible with its owner.
    assert row.primary == unassigned and row.section == "coming_up"
    assert row.others == [joseph_item]
    assert followup_copy.owner_label(joseph_item, me) == "for Joseph"
    assert followup_copy.owner_label(unassigned, me) == "for the team"

    team_rows = followups.rows_for(me, account, scope="team", now=NOW)
    assert len(team_rows["due"]) == 2


@pytest.mark.django_db
def test_customers_are_never_merged_by_name_or_phone(team):
    account, me, _ = team
    a = _contact(account, "+260971000001", "Thandi")
    b = Contact.objects.create(
        account=account, first_name="Thandi", email="t@example.com"
    )
    _fu(account, a, due=NOW - timedelta(hours=1), to=me)
    _fu(account, b, due=NOW - timedelta(hours=1), to=me)

    assert len(followups.rows_for(me, account, now=NOW)["due"]) == 2


@pytest.mark.django_db
def test_policy_followups_are_due_not_waiting_and_a_reply_still_closes_them(team):
    account, me, _ = team
    thandi = _contact(account)
    chat = _chat(account, thandi)
    policy = _fu(
        account,
        thandi,
        due=NOW - timedelta(minutes=1),
        conversation=chat,
        source="policy",
    )

    assert followups.rows_for(me, account, now=NOW)["due"][0].primary == policy
    assert followup_copy.why_label(policy) == "From an automation"

    followups.close_when_team_replies(chat)
    policy.refresh_from_db()
    assert policy.closed_reason == FollowUp.ClosedReason.STAFF_REPLIED


@pytest.mark.django_db
def test_a_followup_without_a_chat_opens_the_contact(team):
    account, me, _ = team
    thandi = _contact(account)
    _fu(account, thandi, due=NOW - timedelta(hours=1), to=me)

    row = followups.rows_for(me, account, now=NOW)["due"][0]
    assert row.target_url == f"/contacts/{thandi.public_id}/"


@pytest.mark.django_db
def test_a_missed_followup_moved_later_waits_in_coming_up(team):
    account, me, _ = team
    thandi = _contact(account)
    _fu(
        account,
        thandi,
        due=NOW + timedelta(hours=1),
        conversation=_chat(account, thandi),
        source="missed",
    )

    assert len(followups.rows_for(me, account, now=NOW)["coming_up"]) == 1


@pytest.mark.django_db
def test_contact_filter_shows_one_customer(team):
    account, me, _ = team
    thandi, grace = _contact(account), _contact(account, "+260971000002", "Grace")
    _fu(account, thandi, due=NOW - timedelta(hours=1), to=me)
    _fu(account, grace, due=NOW - timedelta(hours=1), to=me)

    rows = followups.rows_for(me, account, contact=grace, now=NOW)["due"]
    assert [r.contact for r in rows] == [grace]


# ── The count ────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_count_is_customers_due_now_mine_or_unassigned(team):
    account, me, joseph = team
    thandi = _contact(account)
    _fu(account, thandi, due=NOW - timedelta(hours=2), to=me)
    _fu(account, thandi, due=NOW - timedelta(hours=1))  # same customer: counts once
    _fu(
        account, _contact(account, "+260971000002"), due=NOW + timedelta(hours=1), to=me
    )
    grace = _contact(account, "+260971000003", "Grace")
    _fu(account, grace, due=NOW - timedelta(hours=1), to=joseph)
    done = _fu(
        account, _contact(account, "+260971000004"), due=NOW - timedelta(hours=1), to=me
    )
    followups.close(done)
    cancelled = _fu(
        account, _contact(account, "+260971000005"), due=NOW - timedelta(hours=1), to=me
    )
    followups.close(cancelled, reason=FollowUp.ClosedReason.CANCELLED)

    assert followups.due_for(me, account, now=NOW) == 1
    assert followups.due_for(joseph, account, now=NOW) == 2  # Grace + unassigned Thandi
    assert followups.due_for_team(account, now=NOW) == 2


@pytest.mark.django_db
def test_unassigned_work_counts_for_someone_with_nothing_assigned(team):
    account, _, joseph = team
    _fu(account, _contact(account), due=NOW - timedelta(minutes=1))

    assert followups.due_for(joseph, account, now=NOW) == 1


@pytest.mark.django_db
def test_giving_a_followup_away_moves_it_between_counts(team):
    account, me, joseph = team
    item = _fu(account, _contact(account), due=NOW - timedelta(minutes=1), to=me)

    followups.reassign(item, joseph)

    assert followups.due_for(me, account, now=NOW) == 0
    assert followups.due_for(joseph, account, now=NOW) == 1


@pytest.mark.django_db
def test_tomorrow_morning_is_nine_in_the_business_timezone_and_waits(team, monkeypatch):
    from zoneinfo import ZoneInfo

    account, me, _ = team
    monkeypatch.setattr(
        followups, "business_zone", lambda a: ZoneInfo("Pacific/Auckland")
    )
    item = _fu(account, _contact(account), due=NOW - timedelta(minutes=1), to=me)

    followups.snooze(item, "tomorrow")

    local = item.due_at.astimezone(ZoneInfo("Pacific/Auckland"))
    assert (local.hour, local.minute) == (9, 0)
    assert followups.due_for(me, account) == 0
    assert followups.section_for(item, timezone.now()) == "coming_up"


# ── State changes ────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_done_then_later_leaves_it_closed_and_unmoved(team):
    account, me, _ = team
    item = _fu(account, _contact(account), due=NOW - timedelta(hours=1), to=me)
    stale = FollowUp.objects.get(pk=item.pk)  # a second tab, loaded before Done

    followups.close(item, by=me)
    with pytest.raises(followups.FollowUpClosed) as exc:
        followups.snooze(stale, "tomorrow")

    assert str(exc.value) == followup_copy.ALREADY_DONE
    saved = FollowUp.objects.get(pk=item.pk)
    assert saved.done_at is not None and saved.closed_reason == "done"
    assert saved.due_at == item.due_at and saved.snoozed_count == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change",
    [
        lambda f, u: followups.reassign(f, u),
        lambda f, u: followups.edit(f, note="new"),
        lambda f, u: followups.close(f, by=u, strict=True),
    ],
)
def test_no_change_lands_on_a_closed_followup(team, change):
    account, me, _ = team
    item = _fu(account, _contact(account), due=NOW, to=me)
    stale = FollowUp.objects.get(pk=item.pk)
    followups.close(item, reason=FollowUp.ClosedReason.CANCELLED)

    with pytest.raises(followups.FollowUpClosed):
        change(stale, me)
    saved = FollowUp.objects.get(pk=item.pk)
    assert saved.closed_reason == "cancelled" and saved.note == ""


@pytest.mark.django_db
def test_not_needed_keeps_the_row_as_cancelled(team):
    account, me, _ = team
    item = _fu(account, _contact(account), due=NOW, to=me)

    followups.close(item, by=me, reason=FollowUp.ClosedReason.CANCELLED)

    saved = FollowUp.objects.get(pk=item.pk)
    assert saved.closed_reason == "cancelled" and saved.done_by == me


@pytest.mark.django_db
def test_asking_twice_keeps_one_followup(team):
    account, me, _ = team
    thandi = _contact(account)
    chat = _chat(account, thandi)

    first, made = followups.create(
        account,
        thandi,
        conversation=chat,
        due_at=NOW + timedelta(hours=2),
        source="missed",
    )
    again, made_again = followups.create(
        account,
        thandi,
        conversation=chat,
        due_at=NOW + timedelta(hours=3),
        source="missed",
    )

    assert made and not made_again and first.pk == again.pk
    assert FollowUp.objects.filter(conversation=chat).count() == 1


# ── Messages and access, through the real endpoints ──────────────────────────────────────


@pytest.fixture
def client_me(client, team):
    account, me, _ = team
    client.force_login(me)
    return client


def _toast(resp) -> str:
    return " ".join(str(m) for m in resp.context["messages"]) if resp.context else ""


@pytest.mark.django_db
def test_each_action_says_what_happened(client_me, team):
    account, me, joseph = team
    item = _fu(account, _contact(account), due=NOW, to=me)

    def post(url, data=None):
        return client_me.post(url, data or {}, follow=True)

    later = post(f"/inbox/followups/{item.pk}/snooze/", {"when": "tomorrow"})
    item.refresh_from_db()
    assert followup_copy.moved_to(
        followups.local_label(item.due_at, account)
    ) in _toast(later)
    assert "Follow-up assigned to Joseph." in _toast(
        post(f"/inbox/followups/{item.pk}/reassign/", {"user": joseph.pk})
    )
    assert followup_copy.ASSIGNED_TO_TEAM in _toast(
        post(f"/inbox/followups/{item.pk}/reassign/", {"user": ""})
    )
    assert followup_copy.NOTE_UPDATED in _toast(
        post(f"/inbox/followups/{item.pk}/edit/", {"note": "Send photos"})
    )
    assert followup_copy.MARKED_DONE in _toast(
        post(f"/inbox/followups/{item.pk}/complete/")
    )
    assert followup_copy.ALREADY_DONE in _toast(
        post(f"/inbox/followups/{item.pk}/complete/")
    )
    assert followup_copy.ALREADY_DONE in _toast(
        post(f"/inbox/followups/{item.pk}/snooze/", {"when": "1h"})
    )

    other = _fu(account, _contact(account, "+260971000009"), due=NOW, to=me)
    assert followup_copy.CANCELLED in _toast(
        post(f"/inbox/followups/{other.pk}/cancel/")
    )
    assert FollowUp.objects.get(pk=other.pk).closed_reason == "cancelled"


@pytest.mark.django_db
def test_setting_one_twice_says_the_existing_one_was_kept(client_me, team):
    account, _, _ = team
    chat = _chat(account, _contact(account))
    url = f"/inbox/{chat.public_id}/"

    first = client_me.post(
        url, {"action": "create_followup", "when": "1h"}, follow=True
    )
    second = client_me.post(
        url, {"action": "create_followup", "when": "next_week"}, follow=True
    )

    item = FollowUp.objects.get(conversation=chat)
    when = followups.local_label(item.due_at, account)
    assert followup_copy.set_for(when) in _toast(first)
    assert followup_copy.kept_existing(when) in _toast(second)


@pytest.mark.django_db
def test_another_business_cannot_touch_a_followup(client_me, team):
    other = Account.objects.create(company_name="Elsewhere")
    theirs = _fu(other, _contact(other), due=NOW)

    for path in ("complete/", "snooze/", "cancel/", "reassign/"):
        assert client_me.post(f"/inbox/followups/{theirs.pk}/{path}").status_code == 404
    assert FollowUp.objects.get(pk=theirs.pk).done_at is None


@pytest.mark.django_db
def test_giving_to_someone_outside_the_team_is_refused(client_me, team):
    account, me, _ = team
    outsider = User.objects.create_user("x", "x@example.com", "pw")
    item = _fu(account, _contact(account), due=NOW, to=me)

    resp = client_me.post(
        f"/inbox/followups/{item.pk}/reassign/", {"user": outsider.pk}, follow=True
    )

    assert followup_copy.NOT_ON_TEAM in _toast(resp)
    assert FollowUp.objects.get(pk=item.pk).assigned_to == me
