"""Release 3 (audit 2026-10-19), phase 11: phone-first contacts, list members
and the WhatsApp campaigns API."""

import json
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from apps.api.tests.test_contacts_v1 import api_key  # noqa: F401  (fixture)
from apps.contacts.models import Contact, ContactList
from apps.email.models import EmailApiKey
from apps.whatsapp.models import MessageTemplate, WhatsAppCampaign, WhatsAppContact


def _call(client, key, method, path, body=None, **headers):
    if method == "get":
        return client.get(f"/api/v1{path}", HTTP_X_API_KEY=key, **headers)
    return getattr(client, method)(
        f"/api/v1{path}",
        data=json.dumps(body or {}),
        content_type="application/json",
        HTTP_X_API_KEY=key,
        **headers,
    )


# --- Contacts ------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_a_phone_only_contact_with_attested_opt_in(client, api_key):  # noqa: F811
    key, acc = api_key
    r = _call(
        client,
        key,
        "post",
        "/contacts",
        {
            "phone": "0977123456",
            "first_name": "Ada",
            "whatsapp": "opted_in",
            "attested": True,
        },
    )
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["phone"] == "+260977123456" and body["whatsapp"] == "opted_in"
    assert body["email"] is None and body["tags"] == [] and body["lists"] == []

    bad = _call(client, key, "post", "/contacts", {"phone": "12"})
    assert bad.status_code == 400

    unattested = _call(
        client, key, "patch", f"/contacts/{body['id']}", {"whatsapp": "opted_in"}
    )
    assert unattested.status_code == 200  # already opted in: nothing to attest
    out = _call(
        client, key, "patch", f"/contacts/{body['id']}", {"whatsapp": "opted_out"}
    )
    assert out.json()["whatsapp"] == "opted_out"
    back = _call(
        client,
        key,
        "patch",
        f"/contacts/{body['id']}",
        {"whatsapp": "opted_in", "attested": True},
    )
    assert back.status_code == 409  # STOP wins

    found = _call(client, key, "get", "/contacts?phone=0977123456")
    assert found.json()["total"] == 1


@pytest.mark.django_db
def test_list_members_by_phone_and_id(client, api_key):  # noqa: F811
    key, acc = api_key
    cl = ContactList.objects.create(account=acc, name="VIPs")
    existing = Contact.objects.create(account=acc, phone="+260977000001")

    r = _call(
        client,
        key,
        "post",
        "/lists/vips/members",
        {
            "contacts": [existing.public_id],
            "phones": [
                "0977000002",
                {"phone": "0977000003", "name": "Bwalya Phiri"},
                "nope",
            ],
            "attested": True,
        },
    )
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["added"] == 3 and data["created"] == 2 and len(data["invalid"]) == 1
    assert cl.contacts.count() == 3
    assert Contact.objects.get(phone="+260977000003").last_name == "Phiri"
    assert (
        WhatsAppContact.objects.get(phone_number="+260977000002").opt_in_status
        == "opted_in"
    )

    detail = _call(client, key, "get", "/lists/vips").json()
    assert detail["count"] == 3 and detail["whatsapp"]["opted_in"] == 2

    removed = _call(
        client, key, "delete", "/lists/vips/members", {"phones": ["0977000002"]}
    )
    assert removed.json()["removed"] == 1

    renamed = _call(client, key, "patch", "/lists/vips", {"name": "Best customers"})
    assert renamed.json()["id"] == "best-customers"
    assert _call(client, key, "delete", "/lists/best-customers").status_code == 204

    too_many = _call(
        client, key, "post", "/lists/nope/members", {"phones": ["1"] * 1001}
    )
    assert too_many.status_code == 404  # unknown list first


# --- WhatsApp campaigns -----------------------------------------------------------------


@pytest.fixture
def campaign_key(api_key):  # noqa: F811
    _, acc = api_key
    _, raw = EmailApiKey.create_for_account(
        acc, name="campaigns", scopes=["whatsapp:campaigns"]
    )
    template = MessageTemplate.objects.create(
        account=acc,
        name="Update",
        whatsapp_template_name="order_update",
        content="Hi {{1}}",
        variables=["name"],
        category=MessageTemplate.Category.UTILITY,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    cl = ContactList.objects.create(account=acc, name="VIPs")
    cl.contacts.add(
        Contact.objects.create(account=acc, phone="+260977000001", first_name="Ada")
    )
    with (
        mock.patch("apps.billing.api.usable", return_value=True),
        mock.patch("apps.core.channels.is_enabled", return_value=True),
    ):
        yield raw, acc, template


@pytest.mark.django_db
def test_campaign_api_create_get_and_cancel(client, campaign_key):
    key, acc, template = campaign_key
    body = {
        "name": "Weekend",
        "list": "vips",
        "template": "order_update",
        "variables": {"name": "contact.first_name"},
        "send_at": (timezone.now() + timedelta(days=1)).isoformat(),
        "timezone": "Africa/Lusaka",
    }
    r = _call(
        client, key, "post", "/whatsapp/campaigns", body, HTTP_IDEMPOTENCY_KEY="abc"
    )
    assert r.status_code == 201, r.content
    data = r.json()
    assert data["status"] == "scheduled" and data["list"] == "vips"

    again = _call(
        client, key, "post", "/whatsapp/campaigns", body, HTTP_IDEMPOTENCY_KEY="abc"
    )
    assert again.json()["id"] == data["id"]
    assert WhatsAppCampaign.objects.count() == 1

    got = _call(client, key, "get", f"/whatsapp/campaigns/{data['id']}").json()
    assert "delivery" in got

    cancelled = _call(client, key, "post", f"/whatsapp/campaigns/{data['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    assert (
        _call(
            client, key, "post", f"/whatsapp/campaigns/{data['id']}/pause"
        ).status_code
        == 409
    )


@pytest.mark.django_db
def test_campaign_api_validates_blanks_and_needs_the_scope(
    client,
    api_key,  # noqa: F811
    campaign_key,
):
    key, acc, template = campaign_key
    missing = _call(
        client,
        key,
        "post",
        "/whatsapp/campaigns",
        {"list": "vips", "template": "order_update"},
    )
    assert missing.status_code == 400 and "name" in missing.json()["error"]["message"]

    plain_key, _ = api_key
    assert _call(client, plain_key, "get", "/whatsapp/campaigns").status_code == 403

    sent = _call(
        client,
        key,
        "post",
        "/whatsapp/campaigns",
        {"list": "vips", "template": template.pk, "variables": {"name": "there"}},
    )
    assert sent.json()["status"] == "sending" and sent.json()["recipients"] == 1
