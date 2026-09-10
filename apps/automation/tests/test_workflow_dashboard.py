import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.automation.models import Workflow
from apps.billing.models import Plan, Subscription


@pytest.fixture
def user_account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    Plan.objects.create(slug="p", name="P", price_monthly=Decimal("1"))
    return user, acc


@pytest.mark.django_db
def test_list_and_create_from_template(client, user_account):
    user, acc = user_account
    client.force_login(user)

    assert client.get("/automations/").status_code == 200

    r = client.post("/automations/create/", {"from_template": "welcome-series"})
    assert r.status_code == 302
    wf = Workflow.objects.get(account=acc)
    assert wf.name == "Welcome series"
    assert r["Location"] == f"/automations/{wf.slug}/"


@pytest.mark.django_db
def test_editor_save_and_publish(client, user_account):
    user, acc = user_account
    client.force_login(user)
    wf = Workflow.objects.create(account=acc, name="WF", slug="wf",
                                 definition={"trigger": {"type": "manual"}, "steps": []})

    assert client.get("/automations/wf/").status_code == 200

    good = {"trigger": {"type": "manual"},
            "steps": [{"id": "a", "type": "set_attribute", "key": "x", "value": 1, "next": "b"},
                      {"id": "b", "type": "stop"}]}
    save = client.post("/automations/wf/save/", data=json.dumps({"definition": good}),
                       content_type="application/json")
    assert save.status_code == 200
    assert save.json()["errors"] == []
    wf.refresh_from_db()
    assert len(wf.definition["steps"]) == 2

    pub = client.post("/automations/wf/publish/")
    assert pub.status_code == 302
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.PUBLISHED
    assert wf.version == 2


@pytest.mark.django_db
def test_publish_blocked_on_invalid_definition(client, user_account):
    user, acc = user_account
    client.force_login(user)
    wf = Workflow.objects.create(account=acc, name="Bad", slug="bad",
                                 definition={"trigger": {"type": "manual"},
                                             "steps": [{"id": "a", "type": "send_email", "next": "missing"}]})
    r = client.post("/automations/bad/publish/", follow=True)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.DRAFT
