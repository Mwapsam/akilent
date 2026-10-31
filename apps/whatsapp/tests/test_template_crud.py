"""WhatsApp templates: view, edit, duplicate and delete, and the sync that keeps Meta's id,
rejection reason and wording (templates pass, 2026-10-31). Meta is mocked throughout."""

from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import override_settings

from apps.accounts.models import Account, Membership
from apps.whatsapp.models import MessageTemplate
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.providers import WhatsAppProviderError
from apps.whatsapp.tasks import sync_templates_for_account
from apps.whatsapp.template_builder import (
    TemplateBuilderError,
    can_edit,
    delete_template,
    update_and_resubmit_template,
)

_wa_urls = override_settings(
    ROOT_URLCONF="apps.whatsapp.tests.urls_enabled", WHATSAPP_ENABLED=True
)
_PROVIDER = "apps.whatsapp.template_builder.get_whatsapp_provider"
Status = MessageTemplate.ApprovalStatus


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id="PNID",
        waba_id="WABA1",
        access_token="tok",
        is_active=True,
    )
    client.force_login(user)
    return client, account


def _template(account, status=Status.REJECTED, **kw):
    fields = {
        "name": "order_ready",
        "whatsapp_template_name": "order_ready",
        "language_code": "en",
        "category": "utility",
        "approval_status": status,
        "content": "Hi {{1}}, your order is ready.",
        "variables": ["Customer name"],
        "variable_examples": ["Ada"],
        "meta_template_id": "9001",
    }
    fields.update(kw)
    return MessageTemplate.objects.create(account=account, **fields)


# ── Sync ──────────────────────────────────────────────────────────────────────────────


class _ListProvider:
    def __init__(self, templates):
        self._templates = templates

    def list_templates(self, waba_id):
        return self._templates


def _sync(account, templates):
    with patch(
        "apps.whatsapp.providers.get_whatsapp_provider",
        return_value=_ListProvider(templates),
    ):
        return sync_templates_for_account(account)


@pytest.mark.django_db
def test_sync_brings_in_id_reason_and_wording(logged_in):
    _, account = logged_in
    _sync(
        account,
        [
            {
                "id": "777",
                "name": "made_in_meta",
                "language": "en",
                "category": "MARKETING",
                "status": "REJECTED",
                "rejected_reason": "PROMOTIONAL",
                "components": [
                    {"type": "HEADER", "format": "TEXT", "text": "Big news"},
                    {
                        "type": "BODY",
                        "text": "Hi {{1}}, {{2}} is on sale.",
                        "example": {"body_text": [["Ada", "Sandals"]]},
                    },
                    {"type": "FOOTER", "text": "Reply STOP to opt out"},
                    {
                        "type": "BUTTONS",
                        "buttons": [
                            {"type": "URL", "text": "Shop", "url": "https://x.co"}
                        ],
                    },
                ],
            }
        ],
    )
    t = MessageTemplate.objects.get(
        account=account, whatsapp_template_name="made_in_meta"
    )
    assert t.meta_template_id == "777"
    assert t.approval_status == Status.REJECTED
    assert "Marketing" in t.rejection_reason
    assert t.content == "Hi {{1}}, {{2}} is on sale."
    assert t.header == "Big news" and t.footer == "Reply STOP to opt out"
    assert t.variables == ["1", "2"] and t.variable_examples == ["Ada", "Sandals"]
    assert t.buttons[0]["text"] == "Shop"


@pytest.mark.django_db
def test_sync_keeps_the_blank_names_the_business_gave(logged_in):
    _, account = logged_in
    t = _template(account, status=Status.PENDING, meta_template_id="")
    _sync(
        account,
        [
            {
                "id": "9001",
                "name": "order_ready",
                "language": "en",
                "category": "UTILITY",
                "status": "APPROVED",
                "components": [
                    {
                        "type": "BODY",
                        "text": "Hi {{1}}, your order is ready to collect.",
                    }
                ],
            }
        ],
    )
    t.refresh_from_db()
    assert t.approval_status == Status.APPROVED and t.rejection_reason == ""
    assert t.meta_template_id == "9001"
    assert t.variables == ["Customer name"]
    assert t.content.endswith("ready to collect.")


@pytest.mark.django_db
def test_sync_never_brings_back_a_deleted_template(logged_in):
    _, account = logged_in
    _sync(
        account,
        [{"name": "gone", "language": "en", "status": "DELETED", "components": []}],
    )
    assert not MessageTemplate.objects.filter(whatsapp_template_name="gone").exists()


# ── Edit ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_edit_a_rejected_template_resubmits_it(logged_in):
    _, account = logged_in
    t = _template(account, rejection_reason="It reads like a promotion.")
    with patch(_PROVIDER) as get_provider:
        update_and_resubmit_template(
            t,
            category="marketing",
            body="Hi {{1}}, your order is ready to collect.",
            variable_labels=["Customer name"],
            variable_examples=["Ada"],
        )
    template_id, payload = get_provider.return_value.edit_template.call_args.args
    assert template_id == "9001"
    assert payload["category"] == "MARKETING"
    assert payload["components"][0]["text"].endswith("ready to collect.")
    t.refresh_from_db()
    assert t.approval_status == Status.PENDING and t.rejection_reason == ""
    assert t.category == "marketing"


@pytest.mark.django_db
def test_edit_an_approved_template_keeps_its_category(logged_in):
    _, account = logged_in
    t = _template(account, status=Status.APPROVED)
    with patch(_PROVIDER) as get_provider:
        update_and_resubmit_template(
            t,
            category="marketing",
            body="Hi {{1}}, your order is ready now.",
            variable_labels=["Customer name"],
            variable_examples=["Ada"],
        )
    _, payload = get_provider.return_value.edit_template.call_args.args
    assert "category" not in payload
    t.refresh_from_db()
    assert t.category == "utility" and t.approval_status == Status.PENDING


@pytest.mark.django_db
def test_a_pending_template_cannot_be_edited(logged_in):
    _, account = logged_in
    t = _template(account, status=Status.PENDING)
    assert not can_edit(t)
    with pytest.raises(TemplateBuilderError, match="still reviewing"):
        update_and_resubmit_template(t, body="Hi")


@pytest.mark.django_db
def test_meta_refusing_an_edit_says_why_and_changes_nothing(logged_in):
    _, account = logged_in
    t = _template(account, status=Status.APPROVED)
    with patch(_PROVIDER) as get_provider:
        get_provider.return_value.edit_template.side_effect = WhatsAppProviderError(
            "Meta API 400 [x]: You can only edit a template once a day."
        )
        with pytest.raises(TemplateBuilderError, match="once a day"):
            update_and_resubmit_template(
                t,
                body="Hi {{1}}, changed.",
                variable_labels=["Customer name"],
                variable_examples=["Ada"],
            )
    t.refresh_from_db()
    assert t.approval_status == Status.APPROVED and t.content.endswith("is ready.")


@_wa_urls
@pytest.mark.django_db
def test_edit_page_prefills_and_locks_the_name(logged_in):
    client, account = logged_in
    t = _template(account, status=Status.APPROVED)
    body = client.get(f"/whatsapp/templates/{t.pk}/edit/").content.decode()
    assert "Edit template" in body and "Resubmit for review" in body
    assert 'value="order_ready" required readonly' in body
    assert "data-edit-approved" in body
    assert "Customer name" in body  # the blanks come back


@_wa_urls
@pytest.mark.django_db
def test_edit_view_posts_the_change(logged_in):
    client, account = logged_in
    t = _template(account)
    with patch(_PROVIDER) as get_provider:
        resp = client.post(
            f"/whatsapp/templates/{t.pk}/edit/",
            {
                "name": "something_else",  # ignored: Meta never renames
                "category": "utility",
                "body": "Hi {{1}}, your order is ready to collect.",
                "variable_label": ["Customer name"],
                "variable_example": ["Ada"],
                "header_format": "none",
            },
        )
    assert resp.status_code == 302 and resp.url == f"/whatsapp/templates/{t.pk}/"
    assert get_provider.return_value.edit_template.called
    t.refresh_from_db()
    assert t.whatsapp_template_name == "order_ready"
    assert t.approval_status == Status.PENDING


@_wa_urls
@pytest.mark.django_db
def test_another_accounts_template_is_not_found(logged_in):
    client, _ = logged_in
    other = Account.objects.create(company_name="Other")
    t = _template(other)
    assert client.get(f"/whatsapp/templates/{t.pk}/").status_code == 404
    assert client.get(f"/whatsapp/templates/{t.pk}/edit/").status_code == 404
    assert client.post(f"/whatsapp/templates/{t.pk}/delete/").status_code == 404


# ── Delete ────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_delete_removes_it_at_meta_and_keeps_history(logged_in):
    _, account = logged_in
    t = _template(account, status=Status.APPROVED)
    with patch(_PROVIDER) as get_provider:
        delete_template(t)
    get_provider.return_value.delete_template.assert_called_once_with(
        "WABA1", "order_ready", "9001"
    )
    t.refresh_from_db()
    assert t.approval_status == Status.DELETED
    assert t.name == "order_ready"
    # The name is free again here, once Meta allows it.
    assert t.whatsapp_template_name != "order_ready"


@pytest.mark.django_db
def test_delete_is_refused_while_a_campaign_waits_to_send_it(logged_in):
    from apps.whatsapp.models import WhatsAppCampaign

    _, account = logged_in
    t = _template(account, status=Status.APPROVED)
    WhatsAppCampaign.objects.create(
        account=account, name="December offer", template=t, status="scheduled"
    )
    with patch(_PROVIDER) as get_provider:
        with pytest.raises(TemplateBuilderError, match="December offer"):
            delete_template(t)
    assert not get_provider.return_value.delete_template.called
    t.refresh_from_db()
    assert t.approval_status == Status.APPROVED


@_wa_urls
@pytest.mark.django_db
def test_deleted_templates_leave_the_list_and_the_composer(logged_in):
    client, account = logged_in
    t = _template(account, status=Status.APPROVED)
    with patch(_PROVIDER):
        resp = client.post(f"/whatsapp/templates/{t.pk}/delete/")
    assert resp.status_code == 302
    listing = client.get("/email/templates/?channel=whatsapp").content.decode()
    assert f"/whatsapp/templates/{t.pk}/" not in listing
    from apps.whatsapp.campaign_ui import template_options

    assert t not in template_options(account)  # the campaign composer's choices
    # Still readable, for the campaigns that sent it.
    detail = client.get(f"/whatsapp/templates/{t.pk}/").content.decode()
    assert "This template was deleted" in detail and "data-template-edit" not in detail
    assert client.get(f"/whatsapp/templates/{t.pk}/edit/").status_code == 404


# ── Detail, list, duplicate, builder ──────────────────────────────────────────────────


@_wa_urls
@pytest.mark.django_db
def test_detail_shows_the_reason_preview_and_usage(logged_in):
    client, account = logged_in
    t = _template(account, rejection_reason="It reads like a promotion.")
    body = client.get(f"/whatsapp/templates/{t.pk}/").content.decode()
    assert "It reads like a promotion." in body
    assert "Hi Ada, your order is ready." in body  # preview with the example
    assert "data-template-usage" in body and "No campaigns or automations" in body
    assert "data-template-edit" in body and "data-template-duplicate" in body


@_wa_urls
@pytest.mark.django_db
def test_list_rows_open_the_template_and_show_the_reason(logged_in):
    client, account = logged_in
    t = _template(account, rejection_reason="It reads like a promotion.")
    body = client.get("/email/templates/?channel=whatsapp").content.decode()
    assert f"/whatsapp/templates/{t.pk}/" in body
    assert f"/whatsapp/templates/{t.pk}/edit/" in body
    assert "It reads like a promotion." in body


@_wa_urls
@pytest.mark.django_db
def test_duplicate_prefills_a_copy(logged_in):
    client, account = logged_in
    t = _template(account, status=Status.PENDING)
    body = client.get(f"/whatsapp/templates/new/?from={t.pk}").content.decode()
    assert 'value="order_ready_copy"' in body
    assert "Hi {{1}}, your order is ready." in body


@_wa_urls
@pytest.mark.django_db
def test_builder_uses_the_business_name(logged_in):
    client, _ = logged_in
    body = client.get("/whatsapp/templates/new/").content.decode()
    assert "Acme Store" not in body
    assert "Mwamba Kitchen" in body
    assert body.count("Meta uses the example") == 1
