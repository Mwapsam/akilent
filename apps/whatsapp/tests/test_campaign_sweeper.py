"""Phase A hardening: sweep_stuck_whatsapp_campaigns re-enqueues a campaign
whose chunk chain has gone quiet — either a SENDING campaign whose
last_progress_at is stale, or a QUEUED campaign whose very first chunk never
landed. See apps.whatsapp.tasks.sweep_stuck_whatsapp_campaigns and
apps.whatsapp.models.campaign.WhatsAppCampaign.last_progress_at."""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact, ContactList
from apps.whatsapp.models import (
    MessageTemplate,
    WhatsAppCampaign,
    WhatsAppCampaignRecipient,
    WhatsAppContact,
)
from apps.whatsapp.tasks import sweep_stuck_whatsapp_campaigns

_Status = WhatsAppCampaign.Status


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


@pytest.fixture
def approved_template(account):
    return MessageTemplate.objects.create(
        account=account,
        name="Promo",
        whatsapp_template_name="promo",
        content="Hi {{1}}",
        variables=["name"],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )


def _opted_in_contact(account, i: int) -> Contact:
    contact = Contact.objects.create(account=account, phone=f"+2609720{i:05d}")
    WhatsAppContact.objects.create(
        account=account,
        phone_number=contact.phone,
        contact=contact,
        opt_in_status=WhatsAppContact.OptInStatus.OPTED_IN,
    )
    return contact


def _campaign_with_pending_recipients(
    account, template, *, status, last_progress_at=None, created_at=None
) -> WhatsAppCampaign:
    contact_list = ContactList.objects.create(account=account, name="List")
    contact_list.contacts.add(_opted_in_contact(account, 1))
    campaign = WhatsAppCampaign.objects.create(
        account=account,
        name="X",
        contact_list=contact_list,
        template=template,
        status=status,
        recipient_count=1,
        last_progress_at=last_progress_at,
    )
    WhatsAppCampaignRecipient.objects.create(
        campaign=campaign, contact_id=contact_list.contacts.first().id
    )
    if created_at is not None:
        WhatsAppCampaign.objects.filter(pk=campaign.pk).update(created_at=created_at)
        campaign.refresh_from_db()
    return campaign


@pytest.mark.django_db
def test_sweeper_resumes_sending_campaign_with_stale_progress(
    account, approved_template
):
    stale = timezone.now() - timedelta(minutes=30)
    campaign = _campaign_with_pending_recipients(
        account, approved_template, status=_Status.SENDING, last_progress_at=stale
    )

    with mock.patch("apps.whatsapp.campaigns.send_campaign.delay") as delay:
        result = sweep_stuck_whatsapp_campaigns()

    delay.assert_called_once_with(campaign.id)
    assert result == {"resumed": 1}


@pytest.mark.django_db
def test_sweeper_ignores_sending_campaign_with_recent_progress(
    account, approved_template
):
    recent = timezone.now() - timedelta(minutes=2)
    _campaign_with_pending_recipients(
        account, approved_template, status=_Status.SENDING, last_progress_at=recent
    )

    with mock.patch("apps.whatsapp.campaigns.send_campaign.delay") as delay:
        result = sweep_stuck_whatsapp_campaigns()

    delay.assert_not_called()
    assert result == {"resumed": 0}


@pytest.mark.django_db
def test_sweeper_resumes_queued_campaign_whose_first_chunk_never_ran(
    account, approved_template
):
    stale_created = timezone.now() - timedelta(minutes=30)
    campaign = _campaign_with_pending_recipients(
        account,
        approved_template,
        status=_Status.QUEUED,
        last_progress_at=None,
        created_at=stale_created,
    )

    with mock.patch("apps.whatsapp.campaigns.send_campaign.delay") as delay:
        result = sweep_stuck_whatsapp_campaigns()

    delay.assert_called_once_with(campaign.id)
    assert result == {"resumed": 1}


@pytest.mark.django_db
def test_sweeper_ignores_completed_campaign_regardless_of_staleness(
    account, approved_template
):
    stale = timezone.now() - timedelta(minutes=30)
    _campaign_with_pending_recipients(
        account, approved_template, status=_Status.COMPLETED, last_progress_at=stale
    )

    with mock.patch("apps.whatsapp.campaigns.send_campaign.delay") as delay:
        result = sweep_stuck_whatsapp_campaigns()

    delay.assert_not_called()
    assert result == {"resumed": 0}


@pytest.mark.django_db
def test_sweeper_ignores_sending_campaign_with_no_pending_recipients_left(
    account, approved_template
):
    stale = timezone.now() - timedelta(minutes=30)
    contact_list = ContactList.objects.create(account=account, name="List")
    WhatsAppCampaign.objects.create(
        account=account,
        name="X",
        contact_list=contact_list,
        template=approved_template,
        status=_Status.SENDING,
        last_progress_at=stale,
    )
    # No WhatsAppCampaignRecipient rows at all (or none PENDING) — nothing left to resume.

    with mock.patch("apps.whatsapp.campaigns.send_campaign.delay") as delay:
        result = sweep_stuck_whatsapp_campaigns()

    delay.assert_not_called()
    assert result == {"resumed": 0}


@pytest.mark.django_db
def test_send_campaign_touches_progress_on_each_successful_chunk(
    account, approved_template
):
    from apps.whatsapp import campaigns
    from apps.whatsapp.campaigns import create_and_queue_campaign, send_campaign

    contact_list = ContactList.objects.create(account=account, name="List")
    for i in range(3):
        contact_list.contacts.add(_opted_in_contact(account, i))
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "Hi"},
    )
    assert campaign.last_progress_at is None

    with mock.patch.object(campaigns, "_CAMPAIGN_CHUNK_SIZE", 2):
        send_campaign.run(campaign.id)

    campaign.refresh_from_db()
    assert campaign.last_progress_at is not None
