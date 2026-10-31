"""Stage 7 checks of docs/design/followups-implementation-checklist.md that no earlier stage
pinned: who an automation's follow-up is for (including a teammate who has left), which due
follow-ups are emailed and how often, and that old reminder links still lead somewhere.
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import followups
from apps.conversations.models import Conversation, FollowUp


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def team(db):
    account = Account.objects.create(company_name="Mwamba Kitchen")
    me = User.objects.create_user("me", "me@example.com", "pw", first_name="Ada")
    jo = User.objects.create_user("jo", "jo@example.com", "pw", first_name="Joseph")
    Membership.objects.create(user=me, account=account, role=Membership.Role.OWNER)
    Membership.objects.create(user=jo, account=account, role=Membership.Role.MEMBER)
    return account, me, jo


@pytest.fixture
def chat(team):
    account, me, _ = team
    contact = Contact.objects.create(
        account=account, phone="+260974000001", first_name="Thandi"
    )
    return Conversation.objects.create(
        account=account,
        contact=contact,
        channel=Conversation.Channel.WHATSAPP,
        assigned_to=me,
    )


# ── Who an automation's follow-up is for ────────────────────────────────────────────────


def _run_step(chat, assign_to):
    from apps.automation.models import Workflow, WorkflowRun
    from apps.automation.workflow_engine import enroll

    wf = Workflow.objects.create(
        account=chat.account,
        name="Chase",
        slug=f"chase-{assign_to or 'team'}".replace("@", "-").replace(".", "-"),
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "manual"},
            "steps": [
                {
                    "id": "f",
                    "type": "create_followup",
                    "due_in_hours": 24,
                    "note": "Did {contact} pay?",
                    "assign_to": assign_to,
                }
            ],
        },
    )
    enroll(wf, chat.contact, context={"conversation_id": chat.public_id})
    return WorkflowRun.objects.filter(workflow=wf).latest("pk")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("assign_to", "expected"),
    [("", None), ("assignee", "me"), ("jo@example.com", "jo")],
)
def test_the_step_sets_it_for_the_right_person(team, chat, assign_to, expected):
    _run_step(chat, assign_to)
    f = FollowUp.objects.get(source="workflow")
    owner = f.assigned_to.username if f.assigned_to else None
    assert owner == expected  # "" is the whole team: nobody in particular


@pytest.mark.django_db
def test_a_teammate_who_left_gets_no_follow_up_and_the_owner_is_told_why(team, chat):
    from apps.automation import explain

    _, _, jo = team
    jo.is_active = False
    jo.save(update_fields=["is_active"])
    run = _run_step(chat, "jo@example.com")

    assert not FollowUp.objects.filter(source="workflow").exists()
    assert run.status == run.Status.FAILED
    step = run.step_runs.get(step_id="f")
    said = explain.describe_step(step.step_type, step.status, step.result)
    assert said["ok"] is False
    assert said["text"].startswith("The teammate this follow-up was for is no longer")
    assert "no follow-up was set" in said["text"] and "notify" not in said["text"]


# ── Which due follow-ups are emailed, and how often ─────────────────────────────────────


def _due(chat, *, to=None, source="manual", minutes=-5):
    return FollowUp.objects.create(
        account=chat.account,
        contact=chat.contact,
        conversation=chat,
        due_at=timezone.now() + timedelta(minutes=minutes),
        assigned_to=to,
        source=source,
    )


def _notify():
    with (
        mock.patch("apps.core.realtime.publish"),
        mock.patch("apps.accounts.notifications.notify_team") as email,
    ):
        followups.notify_due()
    return email


@pytest.mark.django_db
def test_only_follow_ups_given_to_someone_who_wants_emails_are_emailed(team, chat):
    _, me, jo = team
    Membership.objects.filter(user=jo).update(followup_emails=False)
    _due(chat)  # whole team: never emailed
    _due(
        chat, to=me, source="missed"
    )  # a customer waiting: the overdue alert covers it
    other = Conversation.objects.create(
        account=chat.account,
        contact=chat.contact,
        channel=Conversation.Channel.INSTAGRAM,
    )
    FollowUp.objects.create(
        account=chat.account,
        contact=chat.contact,
        conversation=other,
        due_at=timezone.now() - timedelta(minutes=5),
        assigned_to=jo,  # switched off
    )
    email = _notify()
    email.assert_not_called()

    mine = Conversation.objects.create(
        account=chat.account,
        contact=chat.contact,
        channel=Conversation.Channel.FACEBOOK,
    )
    FollowUp.objects.create(
        account=chat.account,
        contact=chat.contact,
        conversation=mine,
        due_at=timezone.now() - timedelta(minutes=1),
        assigned_to=me,
    )
    email = _notify()
    email.assert_called_once()
    assert email.call_args.kwargs["to"] == me.email
    assert email.call_args.kwargs["text"].count("•") == 1  # only the one given to Ada


@pytest.mark.django_db
def test_at_most_one_email_an_hour(team, chat):
    _, me, _ = team
    _due(chat, to=me)
    assert _notify().call_count == 1

    second = Conversation.objects.create(
        account=chat.account,
        contact=chat.contact,
        channel=Conversation.Channel.INSTAGRAM,
    )
    FollowUp.objects.create(
        account=chat.account,
        contact=chat.contact,
        conversation=second,
        due_at=timezone.now() - timedelta(minutes=1),
        assigned_to=me,
    )
    assert _notify().call_count == 0  # newly due, but an email went out this hour

    cache.clear()  # the hour has passed
    third = Conversation.objects.create(
        account=chat.account,
        contact=chat.contact,
        channel=Conversation.Channel.FACEBOOK,
    )
    FollowUp.objects.create(
        account=chat.account,
        contact=chat.contact,
        conversation=third,
        due_at=timezone.now() - timedelta(minutes=1),
        assigned_to=me,
    )
    assert _notify().call_count == 1


# ── Old links and old words ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_old_reminder_links_still_lead_somewhere(client, team, chat):
    _, me, _ = team
    client.force_login(me)
    resp = client.get("/inbox/?view=followups&page=2")
    assert resp.status_code == 302 and resp["Location"] == "/inbox/followups/"
    f = _due(chat, to=me, minutes=60)
    # The old Snooze address is what Later posts to: it keeps working.
    resp = client.post(f"/inbox/followups/{f.pk}/snooze/", {"when": "next_week"})
    assert resp.status_code == 302
    f.refresh_from_db()
    assert f.snoozed_count == 1
    for word in ("reminder", "snooze", "remind me"):
        html = client.get("/help/", {"q": word}).content.decode()
        assert "Follow-ups for your team" in html, word
