"""Follow-ups, production-ready (Phase 4 of the 2026-10-24 automation plan).

Business-time due dates, one reminder per kind per conversation, team replies closing
"waiting" reminders, due nudges that fire once, honest completion numbers.
"""

from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import Account, BusinessHours, Membership
from apps.contacts.models import Contact
from apps.conversations import followups
from apps.conversations.models import Conversation, FollowUp


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


@pytest.fixture
def account(db):
    acc = Account.objects.create(company_name="Mwamba Kitchen")
    BusinessHours.objects.create(account=acc, timezone="Africa/Lusaka")  # UTC+2
    return acc


@pytest.fixture
def owner(account):
    user = User.objects.create_user(
        "owner", "owner@example.com", "pw", first_name="Ada"
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user


@pytest.fixture
def conversation(account):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    return Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP
    )


def _create(conversation, **kw):
    kw.setdefault("due_at", timezone.now() + timedelta(hours=1))
    return followups.create(
        conversation.account, conversation.contact, conversation=conversation, **kw
    )


# ── Business time ────────────────────────────────────────────────────────────


def test_tomorrow_morning_is_nine_oclock_where_the_business_is(account):
    noon_utc = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    with mock.patch("django.utils.timezone.now", return_value=noon_utc):
        due = followups.resolve_due_at("tomorrow", account=account)
    assert due == datetime(2026, 10, 26, 7, 0, tzinfo=UTC)  # 09:00 in Lusaka


def test_a_picked_time_is_read_in_business_time(account):
    due = followups.resolve_due_at("custom", "2099-01-02T10:30", account=account)
    assert due == datetime(2099, 1, 2, 8, 30, tzinfo=UTC)


def test_a_picked_time_in_the_past_is_refused(account):
    with pytest.raises(ValueError):
        followups.resolve_due_at("custom", "2000-01-01T10:00", account=account)


def test_labels_say_the_zone(account):
    label = followups.local_label(datetime(2026, 10, 26, 7, 0, tzinfo=UTC), account)
    assert label.startswith("Mon 26 Oct, 09:00")


# ── One reminder per kind ────────────────────────────────────────────────────


def test_a_second_reminder_of_the_same_kind_returns_the_first(conversation, owner):
    first, made = _create(conversation, created_by=owner)
    again, made_again = _create(conversation, created_by=owner)
    assert made and not made_again and again.pk == first.pk
    assert first.assigned_to == owner


def test_asking_again_sooner_moves_it_earlier(conversation):
    first, _ = _create(conversation, due_at=timezone.now() + timedelta(days=2))
    soon = timezone.now() + timedelta(hours=1)
    _create(conversation, due_at=soon)
    first.refresh_from_db()
    assert abs((first.due_at - soon).total_seconds()) < 1


def test_different_kinds_can_coexist(conversation):
    _create(conversation, source="manual")
    _create(conversation, source="missed")
    assert FollowUp.objects.filter(done_at__isnull=True).count() == 2


def test_reopen_is_refused_when_a_copy_is_already_open(conversation):
    first, _ = _create(conversation)
    followups.close(first)
    _create(conversation)
    with pytest.raises(ValueError):
        followups.reopen(first)


# ── Who closes what ──────────────────────────────────────────────────────────


def test_a_team_reply_closes_waiting_reminders_but_not_a_persons(conversation):
    from apps.conversations.services import record_outbound_message

    missed, _ = _create(conversation, source="missed")
    mine, _ = _create(conversation, source="manual", note="Send the quote")
    record_outbound_message(
        conversation=conversation, body="Hi!", timestamp=timezone.now(), status="sent"
    )
    missed.refresh_from_db()
    mine.refresh_from_db()
    assert missed.closed_reason == FollowUp.ClosedReason.STAFF_REPLIED
    assert mine.done_at is None


def test_a_failed_send_does_not_count_as_a_reply(conversation):
    from apps.conversations.services import record_outbound_message

    missed, _ = _create(conversation, source="missed")
    record_outbound_message(
        conversation=conversation, body="Hi!", timestamp=timezone.now(), status="failed"
    )
    missed.refresh_from_db()
    assert missed.done_at is None


# ── Due nudges ───────────────────────────────────────────────────────────────


def test_a_due_reminder_nudges_once_and_emails_its_owner(conversation, owner):
    f, _ = _create(
        conversation, created_by=owner, due_at=timezone.now() - timedelta(minutes=1)
    )
    with (
        mock.patch("apps.core.realtime.publish") as publish,
        mock.patch("apps.accounts.notifications.notify_team") as email,
    ):
        assert followups.notify_due() == 1
        assert followups.notify_due() == 0, "once per due time"
    assert any(c.args[1] == "followup.due" for c in publish.call_args_list)
    email.assert_called_once()
    assert email.call_args.kwargs["to"] == owner.email


def test_snoozing_re_arms_the_nudge(conversation, owner):
    f, _ = _create(
        conversation, created_by=owner, due_at=timezone.now() - timedelta(minutes=1)
    )
    with mock.patch("apps.accounts.notifications.notify_team"):
        followups.notify_due()
    followups.snooze(f, "1h")
    f.refresh_from_db()
    assert f.notified_at is None and f.snoozed_count == 1


def test_no_email_for_people_who_turned_them_off(conversation, owner):
    Membership.objects.filter(user=owner).update(followup_emails=False)
    _create(
        conversation, created_by=owner, due_at=timezone.now() - timedelta(minutes=1)
    )
    with mock.patch("apps.accounts.notifications.notify_team") as email:
        followups.notify_due()
    email.assert_not_called()


def test_missed_reminders_get_no_extra_email(conversation, owner):
    _create(
        conversation,
        source="missed",
        assigned_to=owner,
        due_at=timezone.now() - timedelta(minutes=1),
    )
    with mock.patch("apps.accounts.notifications.notify_team") as email:
        followups.notify_due()
    email.assert_not_called()


# ── Honest numbers ───────────────────────────────────────────────────────────


def test_completion_counts_handled_reminders_only(conversation, owner):
    from apps.conversations.performance import followup_completion

    now = timezone.now()
    for i, reason in enumerate(["done", "cancelled", "customer_replied"]):
        FollowUp.objects.create(
            account=conversation.account,
            contact=conversation.contact,
            due_at=now - timedelta(hours=i + 2),
            done_at=now - timedelta(hours=i + 2),
            closed_reason=reason,
        )
    stats = followup_completion(conversation.account)
    assert stats["due"] == 2  # cancelled is left out
    assert stats["completed"] == 1
    assert stats["on_time_pct"] == 100


# ── Automations and Insights make them too ───────────────────────────────────


def test_an_automation_step_adds_a_reminder(conversation, owner):
    from apps.automation.models import Workflow
    from apps.automation.workflow_engine import enroll

    conversation.assigned_to = owner
    conversation.save(update_fields=["assigned_to"])
    wf = Workflow.objects.create(
        account=conversation.account,
        name="Chase",
        slug="chase",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "manual"},
            "steps": [
                {
                    "id": "remind",
                    "type": "create_followup",
                    "due_in_hours": 48,
                    "note": "Did {contact} pay?",
                    "assign_to": "assignee",
                }
            ],
        },
    )
    enroll(
        wf, conversation.contact, context={"conversation_id": conversation.public_id}
    )
    f = FollowUp.objects.get(source="workflow")
    assert f.assigned_to == owner and f.conversation == conversation
    assert "pay?" in f.note


def test_a_policy_made_from_the_follow_up_insight_can_be_switched_on(owner, account):
    from apps.conversations import insights_views

    assert insights_views._POLICY_ACTION_TYPES["follow_up"] == "create_followup"


def test_recovery_covers_instagram_and_skips_closed_businesses(account):
    from apps.conversations import recovery

    with mock.patch("apps.conversations.recovery.with_activity") as wa:
        wa.return_value.filter.return_value = []
        recovery.create_missed_followups()
    qs_filter = wa.call_args.args[0].query.where
    sql = str(wa.call_args.args[0].query)
    assert "instagram" in sql and "is_active" in sql
    assert qs_filter is not None


# ── Pages ────────────────────────────────────────────────────────────────────


def test_the_page_and_badge(client, conversation, owner):
    client.force_login(owner)
    _create(
        conversation, created_by=owner, due_at=timezone.now() - timedelta(minutes=5)
    )
    page = client.get("/inbox/followups/?list=mine")
    assert page.status_code == 200 and ">Later<" in page.content.decode()
    assert client.get("/inbox/followups/?list=team").status_code == 200
    badge = client.get("/inbox/followups/badge/?toast=1")
    assert ">1<" in badge.content.decode() and "X-Toast" in badge


def test_snooze_from_the_page(client, conversation, owner):
    client.force_login(owner)
    f, _ = _create(
        conversation, created_by=owner, due_at=timezone.now() - timedelta(minutes=5)
    )
    client.post(f"/inbox/followups/{f.pk}/snooze/", {"when": "tomorrow"})
    f.refresh_from_db()
    assert f.due_at > timezone.now() and f.snoozed_count == 1


def test_another_business_cannot_touch_a_reminder(client, conversation):
    stranger = User.objects.create_user("x", "x@example.com", "pw")
    other = Account.objects.create(company_name="Other")
    Membership.objects.create(user=stranger, account=other, role=Membership.Role.OWNER)
    client.force_login(stranger)
    f, _ = _create(conversation)
    assert client.post(f"/inbox/followups/{f.pk}/cancel/").status_code == 404
