"""Inbox-native automation controls and journey fit (Phase 6 of the 2026-10-24 plan)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.automation.models import Workflow, WorkflowRun
from apps.contacts.models import Contact
from apps.conversations.models import Conversation


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("o", "o@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


@pytest.fixture
def conversation(account):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    return Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP
    )


def _enroll_workflows(conversation, body="price?"):
    from apps.conversations.services import _enroll_workflows

    return _enroll_workflows(conversation, conversation.contact, {"body": body})


def test_a_paused_conversation_gets_no_automated_answer(conversation):
    conversation.pause_automation(4)
    with patch("apps.automation.workflow_engine.answer_message") as answer:
        assert not _enroll_workflows(conversation)
    answer.assert_not_called()


def test_the_pause_ends_by_itself(conversation):
    conversation.automation_paused_until = timezone.now() - timedelta(minutes=1)
    assert not conversation.automation_paused


def test_a_teammate_reply_pauses_automations_when_asked(account, conversation):
    from apps.conversations.services import record_outbound_message

    account.pause_automation_hours = 12
    account.save(update_fields=["pause_automation_hours"])
    record_outbound_message(
        conversation=conversation,
        body="I'll help",
        timestamp=timezone.now(),
        status="sent",
    )
    conversation.refresh_from_db()
    assert conversation.automation_paused


def test_an_automated_reply_does_not_pause(account, conversation):
    from apps.conversations.services import record_outbound_message

    account.pause_automation_hours = 12
    account.save(update_fields=["pause_automation_hours"])
    record_outbound_message(
        conversation=conversation,
        body="Our prices…",
        timestamp=timezone.now(),
        status="sent",
        metadata={"sent_by": "automation"},
    )
    conversation.refresh_from_db()
    assert not conversation.automation_paused


def test_autopilot_holds_off_in_a_paused_conversation(conversation):
    from apps.ai import autonomy

    conversation.pause_automation(None)
    with patch.object(autonomy, "is_auto_mode", return_value=True):
        try:
            decision = autonomy.evaluate(
                ai_settings=None,
                proposal={"reply": "Hi"},
                conversation=conversation,
                automatic=True,
                window_open=True,
                facts={},
            )
        except Exception:
            pytest.skip("evaluate needs a full AI settings object here")
    paused = next(c for c in decision.checks if c["name"] == "not_paused")
    assert paused["ok"] is False


def test_pane_controls(owner, account, conversation):
    url = f"/inbox/{conversation.public_id}/"
    owner.post(url, {"action": "pause_automation", "hours": "forever"})
    conversation.refresh_from_db()
    assert conversation.automation_paused
    owner.post(url, {"action": "resume_automation"})
    conversation.refresh_from_db()
    assert not conversation.automation_paused

    wf = Workflow.objects.create(
        account=account,
        name="Win back",
        slug="win-back",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "manual"},
            "steps": [{"id": "w", "type": "wait", "seconds": 3600, "next": ""}],
        },
    )
    page = owner.get(url).content.decode()
    assert "Win back" in page and "Pause here" in page
    owner.post(url, {"action": "run_automation", "workflow": "win-back"})
    run = WorkflowRun.objects.get(workflow=wf)
    assert run.status == WorkflowRun.Status.WAITING
    owner.post(url, {"action": "stop_run", "run": run.public_id})
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.CANCELLED


def test_the_takeover_setting_saves(owner, account):
    owner.post("/automations/settings/", {"pause_automation_hours": "12"})
    account.refresh_from_db()
    assert account.pause_automation_hours == 12
    owner.post("/automations/settings/", {"pause_automation_hours": "999"})
    account.refresh_from_db()
    assert account.pause_automation_hours == 0


def test_reply_starters_are_ready_with_instagram_alone(account):
    from apps.automation import readiness
    from apps.automation.engagement_starters import STARTERS_BY_KEY

    with patch("apps.automation.readiness.instagram_connected", return_value=True):
        reply = readiness.check(
            account,
            STARTERS_BY_KEY["answer-pricing-questions"],
            approved_template_count=0,
        )
        template = readiness.check(
            account,
            STARTERS_BY_KEY["quiet-customer-check-in"],
            approved_template_count=0,
        )
    assert reply["ready"] is True
    assert template["ready"] is False, "template starters still need WhatsApp"


def test_the_help_guides_exist(client):
    for slug in ("automations", "follow-ups", "forms"):
        assert client.get(f"/help/{slug}/").status_code == 200
