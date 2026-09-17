import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.automation.models import Workflow
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailTemplate
from apps.whatsapp.models import MessageTemplate


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
def test_editor_scopes_template_pickers_to_account(client, user_account):
    user, acc = user_account
    client.force_login(user)
    other_acc = Account.objects.create(company_name="Other")

    EmailTemplate.objects.create(account=acc, name="Welcome", slug="welcome", subject="Hi")
    EmailTemplate.objects.create(account=other_acc, name="Not mine", slug="not-mine")
    EmailTemplate.objects.create(account=acc, name="Inactive", slug="inactive", is_active=False)

    MessageTemplate.objects.create(
        account=acc, name="Order update", whatsapp_template_name="order_update",
        content="x", approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    MessageTemplate.objects.create(
        account=other_acc, name="Not mine", whatsapp_template_name="not_mine", content="x",
    )

    wf = Workflow.objects.create(account=acc, name="WF", slug="wf",
                                 definition={"trigger": {"type": "manual"}, "steps": []})
    r = client.get("/automations/wf/")
    assert r.status_code == 200

    email_templates = json.loads(r.context["email_templates_json"])
    email_slugs = [t["slug"] for t in email_templates]
    assert "welcome" in email_slugs
    assert "not-mine" not in email_slugs
    assert "inactive" not in email_slugs

    whatsapp_templates = json.loads(r.context["whatsapp_templates_json"])
    whatsapp_names = [t["name"] for t in whatsapp_templates]
    assert "order_update" in whatsapp_names
    assert "not_mine" not in whatsapp_names
    assert next(t for t in whatsapp_templates if t["name"] == "order_update")["approved"] is True


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
