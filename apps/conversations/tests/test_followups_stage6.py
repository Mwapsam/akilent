"""Stage 6 of docs/design/followups-implementation-checklist.md: notes added, never dropped;
the email switch, the emails, the automation step, onboarding and help in the contract's
words. Throughout: a follow-up is the team's own work, and nothing reads as if a message
went to the customer.
"""

from datetime import timedelta
from unittest import mock

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
    Membership.objects.create(user=me, account=account, role=Membership.Role.OWNER)
    return account, me


@pytest.fixture
def chat(team):
    account, _ = team
    contact = Contact.objects.create(
        account=account, phone="+260973000001", first_name="Thandi"
    )
    return Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP
    )


@pytest.fixture
def signed_in(client, team):
    client.force_login(team[1])
    return client


def _set(chat, note="", hours=24, source="manual"):
    return followups.create(
        chat.account,
        chat.contact,
        conversation=chat,
        due_at=timezone.now() + timedelta(hours=hours),
        note=note,
        source=source,
    )


# ── A new note is added, never dropped or overwritten ──────────────────────────────────


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("first", "second", "saved", "result"),
    [
        ("Send the quote", "Ask about size", "Send the quote\nAsk about size", "added"),
        ("Send the quote", "", "Send the quote", ""),
        ("Send the quote", "   ", "Send the quote", ""),
        ("", "Ask about size", "Ask about size", "added"),
        ("", "", "", ""),
    ],
)
def test_a_second_follow_up_on_a_chat_adds_its_note(chat, first, second, saved, result):
    original, created = _set(chat, note=first)
    assert created
    again, created = _set(chat, note=second)
    assert not created and again.pk == original.pk  # the same follow-up, not a copy
    assert again.note_result == result
    original.refresh_from_db()
    assert original.note == saved
    assert FollowUp.objects.filter(conversation=chat).count() == 1


@pytest.mark.django_db
def test_an_automation_repeating_its_note_does_not_grow_it(chat):
    _set(chat, note="Check in after first order", source="workflow")
    again, _ = _set(chat, note="Check in after first order", source="workflow")
    assert again.note_result == followups.NOTE_ALREADY_THERE
    again.refresh_from_db()
    assert again.note == "Check in after first order"


@pytest.mark.django_db
def test_a_note_that_would_not_fit_is_kept_out_and_said(chat):
    original, _ = _set(chat, note="x" * 250)
    again, _ = _set(chat, note="Ask about size")
    assert again.note_result == followups.NOTE_TOO_LONG
    original.refresh_from_db()
    assert original.note == "x" * 250  # nothing cut, nothing lost silently


@pytest.mark.django_db
def test_the_chat_says_what_became_of_the_note(signed_in, chat):
    url = f"/inbox/{chat.public_id}/"
    signed_in.post(
        url, {"action": "create_followup", "when": "tomorrow", "note": "Send the quote"}
    )
    resp = signed_in.post(
        url,
        {"action": "create_followup", "when": "next_week", "note": "Ask about size"},
        follow=True,
    )
    message = list(resp.context["messages"])[-1]
    assert str(message).endswith("Your note was added to it.")
    assert (
        FollowUp.objects.get(conversation=chat).note == "Send the quote\nAsk about size"
    )

    FollowUp.objects.filter(conversation=chat).update(note="y" * 250)
    resp = signed_in.post(
        url,
        {"action": "create_followup", "when": "next_week", "note": "Ask about size"},
        follow=True,
    )
    message = list(resp.context["messages"])[-1]
    assert message.level_tag == "warning"
    assert "Your note wasn't added" in str(message) and "Edit note" in str(message)


@pytest.mark.django_db
def test_both_lines_show_and_edit_note_keeps_them(signed_in, team, chat):
    account, me = team
    f, _ = _set(chat, note="Send the quote", hours=-1)
    _set(chat, note="Ask about size")
    FollowUp.objects.filter(pk=f.pk).update(assigned_to=me)
    html = signed_in.get("/inbox/followups/?list=mine").content.decode()
    assert "whitespace-pre-line" in html and "Send the quote\nAsk about size" in html
    assert '<textarea name="note"' in html  # a one-line input would join the lines

    signed_in.post(
        f"/inbox/followups/{f.pk}/edit/", {"note": "Send the quote\r\nCall Friday"}
    )
    f.refresh_from_db()
    assert f.note == "Send the quote\nCall Friday"


# ── The email switch and the emails say what really happens ─────────────────────────────


@pytest.mark.django_db
def test_the_email_switch_says_exactly_what_is_emailed(signed_in):
    page = signed_in.get("/inbox/followups/").content.decode()
    assert copy.EMAILS_TOGGLE_LABEL in page and "when mine are due" not in page

    on = signed_in.post("/inbox/followups/emails/", {"on": "1"}, follow=True)
    assert [str(m) for m in on.context["messages"]] == [copy.EMAILS_ON]
    off = signed_in.post("/inbox/followups/emails/", {"on": "0"}, follow=True)
    assert [str(m) for m in off.context["messages"]] == [copy.EMAILS_OFF]
    for text in (copy.EMAILS_ON, copy.EMAILS_OFF):
        assert "reminder" not in text.lower() and "bell" not in text.lower()
    assert (
        "Whole-team follow-ups" in copy.EMAILS_ON and "once an hour" in copy.EMAILS_ON
    )


@pytest.mark.django_db
def test_the_due_email_is_for_the_team_and_says_the_customer_gets_nothing(team, chat):
    _, me = team
    f, _ = _set(chat, note="Send the quote\nAsk about size", hours=-1)
    FollowUp.objects.filter(pk=f.pk).update(assigned_to=me)
    with (
        mock.patch("apps.core.realtime.publish"),
        mock.patch("apps.accounts.notifications.notify_team") as email,
    ):
        followups.notify_due()
    kwargs = email.call_args.kwargs
    assert kwargs["to"] == me.email
    assert kwargs["text"].startswith(copy.EMAIL_INTRO)
    assert "Thandi: Send the quote / Ask about size" in kwargs["text"]
    assert "WhatsApp chat" in kwargs["text"]
    assert "customers never do" in kwargs["reason"]
    assert "reminder" not in (kwargs["text"] + kwargs["reason"]).lower()


# ── Automation step, onboarding, help ───────────────────────────────────────────────────


def test_the_automation_step_is_a_follow_up_for_the_team():
    from apps.automation import builder, explain, labels

    assert labels.STEP_LABELS["create_followup"] == "Set a follow-up for your team"
    catalog = dict(item for group in builder._STEP_GROUPS for item in group[1])
    assert "Nothing is sent to the customer" in catalog["create_followup"]
    said = explain._step_sentence({"type": "create_followup", "due_in_hours": 24})
    done = explain.describe_step("create_followup", "done", {})["text"]
    assert done == "Set a follow-up for your team"
    assert said == "sets a follow-up for your team, due in 1 day"
    for text in (
        labels.STEP_LABELS["create_followup"],
        catalog["create_followup"],
        said,
    ):
        assert "remind" not in text.lower() and "list" not in text.lower()


@pytest.mark.django_db
def test_the_step_editor_offers_who_its_for_not_a_free_text_list(client, team):
    from apps.automation.models import Workflow

    account, me = team
    client.force_login(me)
    Workflow.objects.create(
        account=account,
        name="Chase",
        slug="chase",
        definition={
            "trigger": {"type": "manual"},
            "steps": [{"id": "f", "type": "create_followup", "due_in_hours": 24}],
        },
    )
    html = client.get("/automations/chase/").content.decode()
    step = html[html.index("step.type === 'create_followup'") :]
    step = step[: step.index("lookup_product")]
    assert "Who it&#x27;s for" in step or "Who it's for" in step
    assert '<option value="assignee">Whoever is assigned to the chat</option>' in step
    assert '<option value="">Whole team</option>' in step
    assert f'<option value="{me.email}">Ada</option>' in step
    assert "Whose list" not in step and "Remind in" not in step
    assert "nothing is sent to the customer" in step


def test_onboarding_and_help_use_follow_up_words():
    from apps.accounts import onboarding
    from apps.core import help as help_registry

    step = next(a for a in onboarding.NEXT_ACTIONS if a.key == "set_followup")
    assert "reminder" not in step.desc.lower() and "Nothing is sent" in step.desc
    article = next(a for a in help_registry.ARTICLES if a.slug == "follow-ups")
    assert article.title == "Follow-ups for your team"
    assert "reminder" in article.keywords  # the old word still finds it


@pytest.mark.django_db
def test_the_help_article_reads_in_the_contract_words(signed_in):
    html = signed_in.get("/help/follow-ups/").content.decode()
    assert "Only your team sees it. Nothing is sent to the customer" in html
    assert "Follow up tomorrow" in html and "Suggested follow-up" in html
    assert "Set a follow-up for your team" in html
    assert (
        'called these "reminders"' in html
        or "called these &quot;reminders&quot;" in html
    )
    for old in (
        "Remind me tomorrow",
        "Snooze",
        "Remind your team",
        "Waiting for a reply<",
    ):
        assert old not in html
