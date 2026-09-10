import json
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
                               max_emails_per_month=100, email_apis=True, api_rate_per_min=0)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    _, raw = EmailApiKey.create_for_account(acc, name="k")
    return raw, acc


def _post(client, key, path, body):
    return client.post(f"/api/v1{path}", data=json.dumps(body),
                       content_type="application/json", HTTP_X_API_KEY=key)


@pytest.mark.django_db
def test_create_list_and_fetch_contact(client, api_key):
    key, acc = api_key
    r = _post(client, key, "/contacts", {"email": "a@x.com", "first_name": "Ada",
                                         "attributes": {"tier": "gold"}})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert cid.startswith("con_")

    lst = client.get("/api/v1/contacts", HTTP_X_API_KEY=key)
    assert lst.json()["total"] == 1

    detail = client.get(f"/api/v1/contacts/{cid}", HTTP_X_API_KEY=key)
    assert detail.json()["attributes"]["tier"] == "gold"


@pytest.mark.django_db
def test_contact_create_is_idempotent_upsert(client, api_key):
    key, acc = api_key
    _post(client, key, "/contacts", {"email": "a@x.com"})
    r2 = _post(client, key, "/contacts", {"email": "a@x.com", "first_name": "Ada"})
    assert r2.status_code == 200
    assert Contact.objects.filter(account=acc).count() == 1


@pytest.mark.django_db
def test_import_and_segment_preview(client, api_key):
    key, acc = api_key
    csv_text = "email,country\nz1@x.com,ZM\nz2@x.com,ZM\nu1@x.com,US\n"
    imp = _post(client, key, "/contacts/import",
                {"csv": csv_text, "mapping": {"email": "email", "country": "attr:country"}})
    assert imp.status_code == 202
    assert imp.json()["created"] == 3

    preview = _post(client, key, "/segments/preview", {
        "definition": {"op": "and", "conditions": [
            {"field": "attributes.country", "operator": "eq", "value": "ZM"}]}})
    assert preview.json()["count"] == 2


@pytest.mark.django_db
def test_segment_create_then_list_its_contacts(client, api_key):
    key, acc = api_key
    _post(client, key, "/contacts", {"email": "z@x.com", "attributes": {"country": "ZM"}})
    _post(client, key, "/contacts", {"email": "u@x.com", "attributes": {"country": "US"}})
    created = _post(client, key, "/segments", {
        "name": "Zambia",
        "definition": {"op": "and", "conditions": [
            {"field": "attributes.country", "operator": "eq", "value": "ZM"}]}})
    assert created.status_code == 201
    slug = created.json()["id"]
    rows = client.get(f"/api/v1/segments/{slug}/contacts", HTTP_X_API_KEY=key)
    assert rows.json()["total"] == 1


@pytest.mark.django_db
def test_bad_segment_definition_rejected(client, api_key):
    key, _ = api_key
    r = _post(client, key, "/segments/preview",
              {"definition": {"op": "and", "conditions": [{"field": "ssn", "operator": "eq", "value": "x"}]}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_segment"


@pytest.mark.django_db
def test_unknown_contact_404(client, api_key):
    key, _ = api_key
    assert client.get("/api/v1/contacts/con_missing", HTTP_X_API_KEY=key).status_code == 404


@pytest.mark.django_db
def test_campaign_targets_a_segment(client, api_key):
    key, acc = api_key
    from apps.email.models import EmailApiKey, EmailDomain
    EmailDomain.objects.create(account=acc, domain="mail.acme.test",
                               status=EmailDomain.Status.VERIFIED)
    acc.subscription.plan.bulk_email = True
    acc.subscription.plan.max_bulk_recipients_per_campaign = -1
    acc.subscription.plan.save()
    k = EmailApiKey.objects.get(account=acc)
    k.scopes = ["messages:send", "messages:send:bulk", "templates:manage"]
    k.save(update_fields=["scopes"])

    _post(client, key, "/contacts", {"email": "z1@x.com", "attributes": {"country": "ZM"}})
    _post(client, key, "/contacts", {"email": "z2@x.com", "attributes": {"country": "ZM"}})
    _post(client, key, "/contacts", {"email": "u1@x.com", "attributes": {"country": "US"}})
    seg = _post(client, key, "/segments", {
        "name": "ZM", "definition": {"op": "and", "conditions": [
            {"field": "attributes.country", "operator": "eq", "value": "ZM"}]}})
    slug = seg.json()["id"]

    r = _post(client, key, "/campaigns", {
        "from": "hello@mail.acme.test", "subject": "Hi", "text": "yo", "segment": slug})
    assert r.status_code == 202, r.content
    assert r.json()["recipient_count"] == 2


@pytest.mark.django_db
def test_events_ingest_and_catalog(client, api_key):
    key, acc = api_key
    r = _post(client, key, "/events", {"event": "invoice.paid", "customer": "b@x.com",
                                       "data": {"amount": 999}})
    assert r.status_code == 202, r.content
    assert r.json()["contact"].startswith("con_")

    _post(client, key, "/events", {"event": "invoice.paid", "customer": "c@x.com"})
    _post(client, key, "/events", {"event": "order.shipped", "customer": "b@x.com"})

    cat = client.get("/api/v1/events/catalog", HTTP_X_API_KEY=key).json()["data"]
    names = {row["name"]: row["count"] for row in cat}
    assert names == {"invoice.paid": 2, "order.shipped": 1}


@pytest.mark.django_db
def test_events_ingest_requires_event_name(client, api_key):
    key, _ = api_key
    r = _post(client, key, "/events", {"customer": "b@x.com"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"
