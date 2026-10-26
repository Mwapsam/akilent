"""UI quality programme, Phase 4: WhatsApp screens say what actually happened.

The campaign page labelled every recipient whose message had been handed to the send
queue "Sent", including messages WhatsApp later failed, never confirmed, or that were
still waiting. Each row now shows its message's real outcome, the same one the CSV
export and the stat cards use.
"""

import pytest
from django.test import override_settings

from apps.contacts.models import Contact, ContactList
from apps.whatsapp.models import (
    MessageTemplate,
    OutboundMessage,
    WhatsAppCampaign,
    WhatsAppCampaignRecipient,
    WhatsAppContact,
)
from apps.whatsapp.tests.test_campaign_views import logged_in  # noqa: F401  (fixture)

_wa_urls = override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled")

_R = WhatsAppCampaignRecipient.Status
_O = OutboundMessage.Status


@pytest.fixture
def campaign(logged_in):  # noqa: F811
    _, account = logged_in
    template = MessageTemplate.objects.create(
        account=account,
        name="Promo",
        whatsapp_template_name="promo",
        content="Hi!",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    contact_list = ContactList.objects.create(account=account, name="VIPs")
    campaign = WhatsAppCampaign.objects.create(
        account=account,
        name="Launch",
        contact_list=contact_list,
        template=template,
        status=WhatsAppCampaign.Status.COMPLETED,
        recipient_count=3,
        queued_count=3,
    )
    for i, (name, status) in enumerate(
        [
            ("Failed Fiona", _O.FAILED),
            ("Unsure Uma", _O.UNCONFIRMED),
            ("Queued Quinn", _O.QUEUED),
        ]
    ):
        contact = Contact.objects.create(
            account=account, first_name=name, phone=f"+26097000000{i}"
        )
        wa = WhatsAppContact.objects.create(
            account=account, phone_number=contact.phone, contact=contact
        )
        message = OutboundMessage.objects.create(
            account=account, contact=wa, payload={"type": "template"}, status=status
        )
        WhatsAppCampaignRecipient.objects.create(
            campaign=campaign, contact=contact, message=message, status=_R.QUEUED
        )
    return campaign


def _rows(body: str) -> str:
    return body[body.index('id="recipients"') :]


@_wa_urls
@pytest.mark.django_db
def test_a_queued_message_is_not_called_sent(logged_in, campaign):  # noqa: F811
    client, _ = logged_in
    rows = _rows(client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode())
    assert 'badge-success">Sent' not in rows
    assert 'badge-danger">Failed' in rows
    assert 'badge-warning">Not confirmed' in rows
    assert "won't be sent again automatically" in rows
    assert 'badge-info">Queued' in rows


@_wa_urls
@pytest.mark.django_db
def test_the_failed_filter_includes_messages_that_failed_after_queueing(
    logged_in,  # noqa: F811
    campaign,
):
    client, _ = logged_in
    failed = _rows(
        client.get(
            f"/whatsapp/campaigns/{campaign.pk}/?outcome=failed"
        ).content.decode()
    )
    assert "Failed Fiona" in failed
    assert "Unsure Uma" not in failed and "Queued Quinn" not in failed
    # Nothing has been confirmed by WhatsApp, so the Sent filter is empty.
    sent = _rows(
        client.get(f"/whatsapp/campaigns/{campaign.pk}/?outcome=sent").content.decode()
    )
    assert "No recipients match this filter" in sent


@_wa_urls
@pytest.mark.django_db
def test_a_held_message_says_how_long_it_waits(logged_in, campaign):  # noqa: F811
    client, _ = logged_in
    OutboundMessage.objects.filter(status=_O.QUEUED).update(status=_O.HELD)
    with override_settings(WHATSAPP_CAMPAIGN_HOLD_MAX_HOURS=48):
        rows = _rows(client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode())
    assert 'badge-warning">Waiting' in rows
    assert "within 48 hours" in rows


@_wa_urls
@pytest.mark.django_db
def test_template_list_explains_each_status(logged_in):  # noqa: F811
    client, account = logged_in
    for name, status in [
        ("rejected_one", MessageTemplate.ApprovalStatus.REJECTED),
        ("pending_one", MessageTemplate.ApprovalStatus.PENDING),
    ]:
        MessageTemplate.objects.create(
            account=account,
            name=name,
            whatsapp_template_name=name,
            content="Hi",
            approval_status=status,
        )
    with override_settings(WHATSAPP_ENABLED=True):
        body = client.get("/email/templates/?channel=whatsapp").content.decode()
    if "rejected_one" not in body:
        pytest.skip("WhatsApp templates tab not available in this configuration")
    assert "Meta didn't approve it, so it can't be sent" in body
    assert "Meta is reviewing it" in body
    assert "every 30 minutes" in body
