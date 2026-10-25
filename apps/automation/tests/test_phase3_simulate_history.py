"""Try it, see what happened, fix it (Phase 3 of the 2026-10-24 automation plan)."""

import json
from unittest import mock

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.automation import api as automation_api
from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun
from apps.automation.simulate import simulate
from apps.contacts.models import Contact

MENU = {
    "trigger": {
        "type": "conversation.message_received",
        "match": {"mode": "contains", "any": ["hi"]},
    },
    "steps": [
        {
            "id": "menu",
            "type": "send_buttons",
            "text": "Hi {first_name}! What do you need?",
            "buttons": [{"title": "Prices"}, {"title": "Hours"}],
            "next": "wait",
        },
        {
            "id": "wait",
            "type": "wait_for_reply",
            "routes": {"Prices": "prices", "Hours": "hours"},
            "on_timeout": "",
        },
        {"id": "prices", "type": "reply_text", "text": "From K50.", "next": "tag"},
        {"id": "hours", "type": "reply_text", "text": "8 to 5.", "next": ""},
        {"id": "tag", "type": "add_tag", "tag": "asked-price", "next": ""},
    ],
}


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def contact(account):
    return Contact.objects.create(
        account=account, phone="+260971234567", first_name="Ada"
    )


# ── Simulator ────────────────────────────────────────────────────────────────


def test_a_message_that_does_not_match_explains_why(account):
    out = simulate(account, MENU, message="where are you?")
    assert out["started"] is False and "hi" in out["why_not"]


def test_the_simulator_waits_for_an_answer_then_follows_it(account):
    first = simulate(account, MENU, message="hi")
    assert first["waiting"]["options"] == ["Prices", "Hours"]
    bots = [e for e in first["transcript"] if e["kind"] == "bot"]
    assert bots[0]["text"] == "Hi Ada! What do you need?"
    assert bots[0]["options"] == ["Prices", "Hours"]

    done = simulate(account, MENU, message="hi", replies=["prices"])
    texts = [e["text"] for e in done["transcript"]]
    assert "From K50." in texts
    assert any("asked-price" in t for t in texts)
    assert done["ended"] is True


def test_the_simulator_changes_nothing(account, contact):
    with mock.patch("apps.conversations.actions.run_action") as send:
        simulate(account, MENU, contact=contact, message="hi", replies=["Prices"])
    send.assert_not_called()
    assert not WorkflowRun.objects.exists()
    assert not contact.tags.exists()


def test_no_answer_takes_the_timeout_path(account):
    out = simulate(account, MENU, message="hi", replies=["__timeout__"])
    assert any("didn't answer" in e["text"] for e in out["transcript"])
    assert out["ended"]


# ── Run history and fixes ─────────────────────────────────────────────────────


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


def _failed_run(account, contact):
    wf = Workflow.objects.create(
        account=account,
        name="Tagger",
        slug="tagger",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "manual"},
            "steps": [
                {"id": "tag", "type": "add_tag", "tag": "vip", "next": "done"},
                {"id": "done", "type": "stop"},
            ],
        },
    )
    run = WorkflowRun.objects.create(
        workflow=wf,
        contact=contact,
        status=WorkflowRun.Status.FAILED,
        current_step="tag",
    )
    WorkflowStepRun.objects.create(
        run=run,
        step_id="tag",
        step_type="add_tag",
        status="error",
        result={"error": "boom"},
    )
    return run


def test_retry_runs_the_failed_step_again(account, contact):
    run = _failed_run(account, contact)
    run = automation_api.retry_run(run)
    assert run.status == WorkflowRun.Status.COMPLETED
    assert contact.tags.filter(slug="vip").exists()
    assert run.context["_retries"] == {"tag": 1}


def test_skip_carries_on_after_the_failed_step(account, contact):
    run = _failed_run(account, contact)
    run = automation_api.skip_failed_step(run)
    assert run.status == WorkflowRun.Status.COMPLETED
    assert not contact.tags.exists()
    assert run.step_runs.get(step_id="tag").status == "skipped"


def test_retry_refuses_when_the_customer_is_already_back_in(account, contact):
    run = _failed_run(account, contact)
    WorkflowRun.objects.create(workflow=run.workflow, contact=contact)
    with pytest.raises(automation_api.RunActionError):
        automation_api.retry_run(run)


def test_send_keys_change_on_retry(account, contact):
    from apps.automation.workflow_engine import send_key

    run = _failed_run(account, contact)
    before = send_key(run, "tag")
    run.context = {"_retries": {"tag": 1}}
    assert send_key(run, "tag") != before


def test_the_history_pages_render_and_fix(owner, account, contact):
    run = _failed_run(account, contact)
    assert owner.get("/automations/tagger/runs/?status=failed").status_code == 200
    page = owner.get(f"/automations/runs/{run.public_id}/")
    assert page.status_code == 200 and "Try again" in page.content.decode()
    owner.post(f"/automations/runs/{run.public_id}/retry/")
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED


def test_history_is_private_to_the_business(owner, account):
    other = Account.objects.create(company_name="Other")
    stranger = Contact.objects.create(account=other, phone="+260970000009")
    run = _failed_run(other, stranger)
    assert owner.get(f"/automations/runs/{run.public_id}/").status_code == 404


# ── Manual runs ───────────────────────────────────────────────────────────────


def test_an_automation_can_be_run_for_a_customer(owner, account, contact):
    Workflow.objects.create(
        account=account,
        name="Win back",
        slug="win-back",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "manual"},
            "steps": [{"id": "tag", "type": "add_tag", "tag": "winback"}],
        },
    )
    page = owner.get(f"/contacts/{contact.public_id}/")
    assert "Win back" in page.content.decode()
    owner.post("/automations/win-back/enroll/", {"contact": contact.public_id})
    assert contact.tags.filter(slug="winback").exists()


def test_simulate_endpoint(owner, account):
    Workflow.objects.create(account=account, name="Menu", slug="menu", definition=MENU)
    resp = owner.post(
        "/automations/menu/simulate/",
        json.dumps({"definition": MENU, "message": "hi", "replies": ["Hours"]}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert "8 to 5." in [e["text"] for e in resp.json()["transcript"]]
