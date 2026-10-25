"""Production blockers found in the 2026-10-24 automation audit.

- A run parked on a plain ``wait`` (quiet-customer check-in) kept AI quiet for days.
- The lead-qualification starter card crashed with a KeyError on install.
- Saving a published workflow wrote any definition straight into the live automation.
- A loop stranded the run "active" forever, blocking the customer from re-entering.
"""

import json
from unittest import mock

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import (
    advance_run,
    answer_message,
    enroll,
    validate_definition,
)
from apps.contacts.models import Contact

QUIET = {
    "trigger": {"type": "conversation.message_received"},
    "steps": [
        {"id": "wait", "type": "wait", "seconds": 3 * 86400, "next": "stop"},
        {"id": "stop", "type": "stop"},
    ],
}

BROKEN = {
    "trigger": {"type": "manual"},
    "steps": [{"id": "a", "type": "wait", "seconds": 60, "next": "nowhere"}],
}

LOOP = {
    "trigger": {"type": "manual"},
    "steps": [
        {"id": "a", "type": "add_tag", "tag": "x", "next": "b"},
        {"id": "b", "type": "add_tag", "tag": "y", "next": "a"},
    ],
}


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971234567")


def _workflow(account, definition, slug="wf"):
    return Workflow.objects.create(
        account=account,
        name=slug,
        slug=slug,
        status=Workflow.Status.PUBLISHED,
        definition=definition,
    )


def _answer(contact, body="Hi"):
    with mock.patch("apps.automation.workflow_engine._run_reply_text", return_value={}):
        return answer_message(
            contact.account_id, contact, context={"message": {"body": body}}
        )


def test_a_run_waiting_on_a_timer_does_not_keep_ai_quiet(account, contact):
    _workflow(account, QUIET)
    assert _answer(contact) is False
    assert WorkflowRun.objects.get().status == WorkflowRun.Status.WAITING
    assert _answer(contact, "Any update?") is False, "AI must answer during the wait"


def test_earlier_sends_of_a_still_waiting_run_do_not_count_again(account, contact):
    _workflow(
        account,
        {
            "trigger": {"type": "conversation.message_received"},
            "steps": [
                {
                    "id": "thanks",
                    "type": "reply_text",
                    "text": "Thanks!",
                    "next": "wait",
                },
                {"id": "wait", "type": "wait", "seconds": 86400, "next": "stop"},
                {"id": "stop", "type": "stop"},
            ],
        },
    )
    assert _answer(contact) is True, "the first message was answered"
    assert _answer(contact, "One more thing") is False


def test_a_run_waiting_for_the_customers_reply_still_counts(account, contact):
    _workflow(account, QUIET)
    with mock.patch(
        "apps.automation.workflow_engine.waiting_on_customer", return_value=True
    ):
        assert _answer(contact) is True


def test_validation_rejects_a_loop():
    assert any("loop" in e["message"] for e in validate_definition(LOOP))


def test_validation_accepts_branches_that_rejoin():
    errors = validate_definition(
        {
            "trigger": {"type": "manual"},
            "steps": [
                {
                    "id": "b",
                    "type": "branch",
                    "field": "tag",
                    "operator": "eq",
                    "value": "vip",
                    "on_true": "t1",
                    "on_false": "t2",
                },
                {"id": "t1", "type": "add_tag", "tag": "x", "next": "end"},
                {"id": "t2", "type": "add_tag", "tag": "y", "next": "end"},
                {"id": "end", "type": "stop"},
            ],
        }
    )
    assert not [e for e in errors if "loop" in e["message"]]


def test_a_looping_run_fails_instead_of_staying_active_forever(account, contact):
    wf = _workflow(account, LOOP)
    with mock.patch("apps.automation.workflow_engine._alert_failure") as alert:
        run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.FAILED
    alert.assert_called_once()
    assert enroll(wf, contact) is not None, "the customer can enter again later"


def test_a_long_chain_is_continued_by_the_sweeper(account, contact):
    steps = [
        {"id": f"s{i}", "type": "add_tag", "tag": f"t{i % 5}", "next": f"s{i + 1}"}
        for i in range(60)
    ] + [{"id": "s60", "type": "stop"}]
    wf = _workflow(account, {"trigger": {"type": "manual"}, "steps": steps})
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.ACTIVE
    assert run.next_due_at is not None, "a due time lets run_due pick it back up"
    advance_run(run)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED


@pytest.fixture
def owner_client(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


def test_lead_qualification_card_installs(owner_client, account):
    resp = owner_client.post(
        "/automations/starters/install/",
        {"starter": "lead-qualification-questionnaire"},
    )
    assert resp.status_code == 302
    wf = Workflow.objects.get(account=account, slug="lead-qualification-questionnaire")
    assert wf.status == Workflow.Status.PUBLISHED
    texts = [s.get("text", "") for s in wf.definition["steps"]]
    assert any("didn't finish" in t for t in texts), "timeouts aren't 'has answered'"


def _save(client, slug, definition):
    return client.post(
        f"/automations/{slug}/save/",
        json.dumps({"definition": definition}),
        content_type="application/json",
    )


def test_editing_a_live_workflow_holds_changes_until_publish(owner_client, account):
    wf = _workflow(account, QUIET, slug="live")
    resp = _save(owner_client, "live", BROKEN)
    assert resp.status_code == 200 and resp.json()["has_changes"] is True
    wf.refresh_from_db()
    assert wf.definition == QUIET, "the running automation is untouched"
    assert wf.draft_definition == BROKEN

    owner_client.post("/automations/live/publish/")
    wf.refresh_from_db()
    assert wf.definition == QUIET, "a broken edit can't be published"


def test_saving_a_draft_with_errors_is_allowed(owner_client, account):
    wf = Workflow.objects.create(account=account, name="d", slug="d", definition=QUIET)
    assert _save(owner_client, "d", BROKEN).status_code == 200
    wf.refresh_from_db()
    assert wf.definition == BROKEN
