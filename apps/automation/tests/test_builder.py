"""The visual builder: what it offers, live checking, and the page itself."""

import json

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.automation import builder
from apps.automation.models import Workflow

DEF = {
    "trigger": {"type": "conversation.message_received"},
    "steps": [
        {
            "id": "reply-1",
            "type": "reply_text",
            "text": "Hi {first_name}!",
            "next": "stop-1",
        },
        {"id": "stop-1", "type": "stop"},
    ],
}


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


def test_the_palette_explains_what_cannot_be_used_yet(account):
    catalog = {c["type"]: c for c in builder.step_catalog(account)}
    assert catalog["send_whatsapp"]["available"] is False
    assert "WhatsApp" in catalog["send_whatsapp"]["reason"]
    assert catalog["add_tag"]["available"] is True
    assert catalog["webhook"]["advanced"] is True


def test_the_palette_offers_only_steps_the_engine_runs(account):
    from apps.automation.workflow_engine import _STEP_TYPES

    assert {c["type"] for c in builder.step_catalog(account)} <= _STEP_TYPES


def test_every_engine_trigger_can_be_picked():
    from apps.automation.workflow_engine import _TRIGGER_TYPES

    assert {t["type"] for t in builder.trigger_catalog()} == _TRIGGER_TYPES


def test_the_editor_renders(owner, account):
    Workflow.objects.create(account=account, name="Hi", slug="hi", definition=DEF)
    resp = owner.get("/automations/hi/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "What should happen next?" in html and "In plain words" in html


def test_validate_checks_without_saving(owner, account):
    wf = Workflow.objects.create(account=account, name="Hi", slug="hi", definition=DEF)
    broken = {
        **DEF,
        "steps": [{"id": "a", "type": "reply_text", "text": "x", "next": "gone"}],
    }
    resp = owner.post(
        "/automations/hi/validate/",
        json.dumps({"definition": broken}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert any("unknown step" in e["message"] for e in body["errors"])
    assert body["summary"]["when"]
    wf.refresh_from_db()
    assert wf.definition == DEF


def test_priority_is_saved(owner, account):
    wf = Workflow.objects.create(account=account, name="Hi", slug="hi", definition=DEF)
    owner.post(
        "/automations/hi/save/",
        json.dumps({"definition": DEF, "priority": 5}),
        content_type="application/json",
    )
    wf.refresh_from_db()
    assert wf.priority == 5
