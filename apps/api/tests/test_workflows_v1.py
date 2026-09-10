from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.contacts.models import Contact
from apps.email.models import EmailApiKey


@pytest.fixture
def api_key(db):
    user = User.objects.create_user("o", "o@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"),
                               max_emails_per_month=100, email_apis=True, email_templates=True,
                               api_rate_per_min=0)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    k, raw = EmailApiKey.create_for_account(acc, name="k")
    k.scopes = ["messages:send", "messages:send:bulk"]
    k.save(update_fields=["scopes"])
    return raw, acc


_DEF = {
    "trigger": {"type": "manual"},
    "steps": [
        {"id": "a", "type": "set_attribute", "key": "onboarded", "value": True, "next": "b"},
        {"id": "b", "type": "stop"},
    ],
}


@pytest.mark.django_db
def test_workflow_crud_publish_and_enroll(client, api_key):
    key, acc = api_key

    created = client.post("/api/v1/workflows", data={"name": "Welcome", "definition": _DEF},
                          content_type="application/json", HTTP_X_API_KEY=key)
    assert created.status_code == 201
    slug = created.json()["id"]
    assert created.json()["status"] == "draft"

    lst = client.get("/api/v1/workflows", HTTP_X_API_KEY=key)
    assert [w["id"] for w in lst.json()["data"]] == [slug]

    pub = client.post(f"/api/v1/workflows/{slug}/publish", HTTP_X_API_KEY=key)
    assert pub.status_code == 200
    assert pub.json()["status"] == "published"
    assert pub.json()["version"] == 2

    c = Contact.objects.create(account=acc, email="dev@acme.com")
    enrolled = client.post(f"/api/v1/workflows/{slug}/runs", data={"contact": "dev@acme.com"},
                           content_type="application/json", HTTP_X_API_KEY=key)
    assert enrolled.status_code == 202
    run_id = enrolled.json()["id"]
    assert enrolled.json()["status"] == "completed"

    c.refresh_from_db()
    assert c.attributes.get("onboarded") is True

    detail = client.get(f"/api/v1/workflow-runs/{run_id}", HTTP_X_API_KEY=key)
    assert detail.status_code == 200
    assert {s["step_id"] for s in detail.json()["steps"]} == {"a", "b"}


@pytest.mark.django_db
def test_workflow_template_catalog_and_create_from_template(client, api_key):
    key, acc = api_key

    cat = client.get("/api/v1/workflows/templates", HTTP_X_API_KEY=key)
    assert cat.status_code == 200
    ids = {t["id"] for t in cat.json()["data"]}
    assert {"welcome-series", "re-engagement", "post-purchase"} <= ids

    created = client.post("/api/v1/workflows", data={"from_template": "welcome-series"},
                          content_type="application/json", HTTP_X_API_KEY=key)
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Welcome series"
    assert body["definition"]["trigger"] == {"type": "contact.created"}

    # the copied definition is structurally valid and publishes
    pub = client.post(f"/api/v1/workflows/{body['id']}/publish", HTTP_X_API_KEY=key)
    assert pub.status_code == 200


@pytest.mark.django_db
def test_create_from_unknown_template_404(client, api_key):
    key, _ = api_key
    r = client.post("/api/v1/workflows", data={"from_template": "nope"},
                    content_type="application/json", HTTP_X_API_KEY=key)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_publish_rejects_invalid_definition(client, api_key):
    key, _ = api_key
    bad = {"trigger": {"type": "manual"},
           "steps": [{"id": "a", "type": "send_email", "next": "missing"}]}
    created = client.post("/api/v1/workflows", data={"name": "Bad", "definition": bad},
                          content_type="application/json", HTTP_X_API_KEY=key)
    slug = created.json()["id"]
    pub = client.post(f"/api/v1/workflows/{slug}/publish", HTTP_X_API_KEY=key)
    assert pub.status_code == 400
    assert pub.json()["error"]["code"] == "invalid_workflow"
    assert pub.json()["error"]["details"]


@pytest.mark.django_db
def test_enroll_requires_published(client, api_key):
    key, acc = api_key
    Contact.objects.create(account=acc, email="x@acme.com")
    created = client.post("/api/v1/workflows", data={"name": "Draft wf", "definition": _DEF},
                          content_type="application/json", HTTP_X_API_KEY=key)
    slug = created.json()["id"]
    r = client.post(f"/api/v1/workflows/{slug}/runs", data={"contact": "x@acme.com"},
                    content_type="application/json", HTTP_X_API_KEY=key)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "workflow_not_published"
