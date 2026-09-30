"""R1.5c: WhatsApp campaigns — bounded, restart-safe fan-out via
WhatsAppCampaignRecipient. See apps.whatsapp.models.campaign and the plan doc
for the processing-vs-delivery split these tests pin."""

from unittest import mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact, ContactList
from apps.whatsapp import campaigns
from apps.whatsapp.campaigns import (
    CampaignError,
    campaign_delivery_stats,
    create_and_queue_campaign,
    send_campaign,
)
from apps.whatsapp.models import (
    MessageLog,
    MessageTemplate,
    OutboundMessage,
    WhatsAppCampaign,
    WhatsAppCampaignRecipient,
    WhatsAppContact,
)

_Status = WhatsAppCampaignRecipient.Status
_SkipReason = WhatsAppCampaignRecipient.SkipReason


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


@pytest.fixture
def approved_template(account):
    return MessageTemplate.objects.create(
        account=account,
        name="Promo",
        whatsapp_template_name="promo",
        content="Hi {{1}}, check out our sale!",
        variables=["name"],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )


def _opted_in_contact(account, i: int) -> Contact:
    contact = Contact.objects.create(account=account, phone=f"+2609710{i:05d}")
    WhatsAppContact.objects.create(
        account=account,
        phone_number=contact.phone,
        contact=contact,
        opt_in_status=WhatsAppContact.OptInStatus.OPTED_IN,
    )
    return contact


@pytest.fixture
def contact_list_with_two_customers(account):
    contact_list = ContactList.objects.create(account=account, name="VIPs")
    for i in range(2):
        contact_list.contacts.add(_opted_in_contact(account, i))
    return contact_list


# --- Creation: rows up front, no fan-out inline ---------------------------


@pytest.mark.django_db
def test_create_and_queue_campaign_creates_pending_rows_and_no_messages_yet(
    account, approved_template
):
    contact_list = ContactList.objects.create(account=account, name="Everyone")
    for i in range(50):
        contact_list.contacts.add(_opted_in_contact(account, i))

    # Deliberately not wrapped in django_capture_on_commit_callbacks: the
    # on_commit-scheduled send_campaign.delay must not have run yet.
    campaign = create_and_queue_campaign(
        account=account,
        name="Big list",
        contact_list=contact_list,
        template_id=approved_template.id,
    )

    assert campaign.status == WhatsAppCampaign.Status.QUEUED
    assert campaign.recipient_count == 50
    assert (
        WhatsAppCampaignRecipient.objects.filter(
            campaign=campaign, status=_Status.PENDING
        ).count()
        == 50
    )
    assert not OutboundMessage.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_create_campaign_rejects_unapproved_template(
    account, contact_list_with_two_customers
):
    draft = MessageTemplate.objects.create(
        account=account,
        name="Draft",
        whatsapp_template_name="draft",
        content="x",
        approval_status=MessageTemplate.ApprovalStatus.DRAFT,
    )
    with pytest.raises(CampaignError, match="approved"):
        create_and_queue_campaign(
            account=account,
            name="X",
            contact_list=contact_list_with_two_customers,
            template_id=draft.id,
        )
    assert not WhatsAppCampaign.objects.exists()


@pytest.mark.django_db
def test_create_campaign_rejects_empty_list(account, approved_template):
    empty_list = ContactList.objects.create(account=account, name="Empty")
    with pytest.raises(CampaignError, match="empty"):
        create_and_queue_campaign(
            account=account,
            name="X",
            contact_list=empty_list,
            template_id=approved_template.id,
        )


# --- Chunking + restart safety ---------------------------------------------


@pytest.mark.django_db
def test_send_campaign_processes_in_bounded_chunks(account, approved_template):
    contact_list = ContactList.objects.create(account=account, name="Five")
    for i in range(5):
        contact_list.contacts.add(_opted_in_contact(account, i))
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "Hi"},
    )

    with mock.patch.object(campaigns, "_CAMPAIGN_CHUNK_SIZE", 2):
        send_campaign.run(campaign.id)
        assert (
            WhatsAppCampaignRecipient.objects.filter(
                campaign=campaign, status=_Status.QUEUED
            ).count()
            == 2
        )
        assert (
            WhatsAppCampaignRecipient.objects.filter(
                campaign=campaign, status=_Status.PENDING
            ).count()
            == 3
        )

        send_campaign.run(campaign.id)
        assert (
            WhatsAppCampaignRecipient.objects.filter(
                campaign=campaign, status=_Status.QUEUED
            ).count()
            == 4
        )

        send_campaign.run(campaign.id)
        assert (
            WhatsAppCampaignRecipient.objects.filter(
                campaign=campaign, status=_Status.QUEUED
            ).count()
            == 5
        )

    campaign.refresh_from_db()
    assert campaign.status == WhatsAppCampaign.Status.COMPLETED
    assert campaign.queued_count == 5


@pytest.mark.django_db
def test_restart_only_touches_pending_rows(account, approved_template):
    contact_list = ContactList.objects.create(account=account, name="Mixed")
    for i in range(5):
        contact_list.contacts.add(_opted_in_contact(account, i))
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "Hi"},
    )

    with mock.patch.object(campaigns, "_CAMPAIGN_CHUNK_SIZE", 2):
        send_campaign.run(campaign.id)  # 2 QUEUED, 3 PENDING

    already_queued = list(
        WhatsAppCampaignRecipient.objects.filter(
            campaign=campaign, status=_Status.QUEUED
        )
        .order_by("pk")
        .values("pk", "message_id", "status")
    )
    assert len(already_queued) == 2

    send_campaign.run(campaign.id)  # should only touch the 3 remaining PENDING

    untouched = list(
        WhatsAppCampaignRecipient.objects.filter(
            pk__in=[r["pk"] for r in already_queued]
        )
        .order_by("pk")
        .values("pk", "message_id", "status")
    )
    assert untouched == already_queued
    assert not WhatsAppCampaignRecipient.objects.filter(
        campaign=campaign, status=_Status.PENDING
    ).exists()


@pytest.mark.django_db
def test_bulk_contact_lookup_does_not_scale_with_recipient_count(
    account, approved_template
):
    """Pins the bulk WhatsAppContact lookup: query count for a chunk of N
    recipients must not grow with N (no per-recipient .filter().first())."""
    contact_list = ContactList.objects.create(account=account, name="Many")
    for i in range(20):
        contact_list.contacts.add(_opted_in_contact(account, i))
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "Hi"},
    )

    with CaptureQueriesContext(connection) as ctx:
        send_campaign.run(campaign.id)
    # A handful of fixed queries (campaign fetch, chunk select, contact
    # lookup, bulk_create, bulk_update, counters, completion check) — not
    # one-per-recipient. 20 recipients would blow well past this if the
    # per-contact N+1 ever came back.
    assert len(ctx.captured_queries) < 20


# --- Personalization ---------------------------------------------------


@pytest.mark.django_db
def test_variable_mapping_resolves_contact_field(account, approved_template):
    contact_list = ContactList.objects.create(account=account, name="List")
    contact = _opted_in_contact(account, 1)
    contact.first_name = "Ada"
    contact.save(update_fields=["first_name"])
    contact_list.contacts.add(contact)

    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "contact.first_name"},
    )
    send_campaign.run(campaign.id)

    msg = OutboundMessage.objects.get(account=account)
    assert msg.payload["params"] == {"name": "Ada"}


@pytest.mark.django_db
def test_variable_mapping_resolves_account_field(account, approved_template):
    account.company_name = "Acme Traders"
    account.save(update_fields=["company_name"])
    contact_list = ContactList.objects.create(account=account, name="List")
    contact_list.contacts.add(_opted_in_contact(account, 1))

    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "account.company_name"},
    )
    send_campaign.run(campaign.id)

    msg = OutboundMessage.objects.get(account=account)
    assert msg.payload["params"] == {"name": "Acme Traders"}


@pytest.mark.django_db
def test_variable_mapping_resolves_business_fact(account, approved_template):
    contact_list = ContactList.objects.create(account=account, name="List")
    contact_list.contacts.add(_opted_in_contact(account, 1))
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "business.location"},
    )

    with mock.patch("apps.accounts.api.business_fact", return_value="Lusaka"):
        send_campaign.run(campaign.id)

    msg = OutboundMessage.objects.get(account=account)
    assert msg.payload["params"] == {"name": "Lusaka"}


@pytest.mark.django_db
def test_variable_mapping_resolves_literal_text(account, approved_template):
    contact_list = ContactList.objects.create(account=account, name="List")
    contact_list.contacts.add(_opted_in_contact(account, 1))
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "Valued customer"},
    )
    send_campaign.run(campaign.id)

    msg = OutboundMessage.objects.get(account=account)
    assert msg.payload["params"] == {"name": "Valued customer"}


@pytest.mark.django_db
def test_variable_mapping_uses_fallback_when_contact_field_is_empty(
    account, approved_template
):
    contact_list = ContactList.objects.create(account=account, name="List")
    contact_list.contacts.add(_opted_in_contact(account, 1))  # no first_name set
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "contact.first_name"},
        variable_fallbacks={"name": "there"},
    )
    send_campaign.run(campaign.id)

    msg = OutboundMessage.objects.get(account=account)
    assert msg.payload["params"] == {"name": "there"}


@pytest.mark.django_db
def test_missing_value_with_no_fallback_skips_recipient(account, approved_template):
    contact_list = ContactList.objects.create(account=account, name="List")
    contact_list.contacts.add(
        _opted_in_contact(account, 1)
    )  # no first_name, no fallback
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "contact.first_name"},
    )
    send_campaign.run(campaign.id)

    assert not OutboundMessage.objects.filter(account=account).exists()
    recipient = WhatsAppCampaignRecipient.objects.get(campaign=campaign)
    assert recipient.status == _Status.SKIPPED
    assert recipient.skip_reason == _SkipReason.MISSING_VALUE
    campaign.refresh_from_db()
    assert campaign.skipped_count == 1


# --- Skip reasons ------------------------------------------------------


@pytest.mark.django_db
def test_campaign_skips_opted_out_and_missing_whatsapp_contacts(
    account, approved_template
):
    contact_list = ContactList.objects.create(account=account, name="Everyone")
    opted_out_contact = Contact.objects.create(account=account, phone="+260971000010")
    WhatsAppContact.objects.create(
        account=account,
        phone_number=opted_out_contact.phone,
        contact=opted_out_contact,
        opt_in_status=WhatsAppContact.OptInStatus.OPTED_OUT,
    )
    no_whatsapp_contact = Contact.objects.create(
        account=account, phone="+260971000011"
    )  # no WhatsAppContact row
    contact_list.contacts.add(opted_out_contact, no_whatsapp_contact)

    campaign = create_and_queue_campaign(
        account=account,
        name="Blast",
        contact_list=contact_list,
        template_id=approved_template.id,
        variable_mapping={"name": "Hi"},
    )
    send_campaign.run(campaign.id)

    campaign.refresh_from_db()
    assert campaign.queued_count == 0
    assert campaign.skipped_count == 2
    assert campaign.status == WhatsAppCampaign.Status.COMPLETED
    assert not OutboundMessage.objects.filter(account=account).exists()

    reasons = set(
        WhatsAppCampaignRecipient.objects.filter(campaign=campaign).values_list(
            "skip_reason", flat=True
        )
    )
    assert reasons == {_SkipReason.OPTED_OUT, _SkipReason.NO_WHATSAPP_IDENTITY}


# --- Status-layer invariant: never write recipient.status=FAILED for a --
# --- Meta delivery outcome; that only ever lives on OutboundMessage/MessageLog.


@pytest.mark.django_db
def test_a_meta_delivery_failure_never_changes_recipient_status(
    account, approved_template, contact_list_with_two_customers
):
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list_with_two_customers,
        template_id=approved_template.id,
        variable_mapping={"name": "Hi"},
    )
    send_campaign.run(campaign.id)

    recipient = WhatsAppCampaignRecipient.objects.filter(campaign=campaign).first()
    assert recipient.status == _Status.QUEUED
    recipient.message.status = OutboundMessage.Status.FAILED
    recipient.message.save(update_fields=["status"])

    recipient.refresh_from_db()
    assert recipient.status == _Status.QUEUED  # unchanged by the delivery outcome


# --- Delivery stats: OutboundMessage.status authoritative for FAILED -------


@pytest.mark.django_db
def test_delivery_stats_bucket_by_recipient_message_status(
    account, approved_template, contact_list_with_two_customers
):
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list_with_two_customers,
        template_id=approved_template.id,
        variable_mapping={"name": "Hi"},
    )
    send_campaign.run(campaign.id)

    recipients = list(WhatsAppCampaignRecipient.objects.filter(campaign=campaign))
    assert len(recipients) == 2

    conversation = _make_conversation(recipients[0].message.contact)
    log = MessageLog.objects.create(
        account=account,
        conversation=conversation,
        contact=recipients[0].message.contact,
        direction=MessageLog.Direction.OUTBOUND,
        status=MessageLog.Status.SENT,
        timestamp=timezone.now(),
    )
    recipients[0].message.message_log = log
    recipients[0].message.status = OutboundMessage.Status.SENT
    recipients[0].message.save(update_fields=["message_log", "status"])

    stats = campaign_delivery_stats(campaign)
    assert stats == {"sent": 1, "delivered": 0, "read": 0, "failed": 0, "pending": 1}


@pytest.mark.django_db
def test_delivery_stats_prefer_outbound_failed_over_stale_delivered_log(
    account, approved_template, contact_list_with_two_customers
):
    campaign = create_and_queue_campaign(
        account=account,
        name="X",
        contact_list=contact_list_with_two_customers,
        template_id=approved_template.id,
        variable_mapping={"name": "Hi"},
    )
    send_campaign.run(campaign.id)
    recipient = WhatsAppCampaignRecipient.objects.filter(campaign=campaign).first()

    conversation = _make_conversation(recipient.message.contact)
    log = MessageLog.objects.create(
        account=account,
        conversation=conversation,
        contact=recipient.message.contact,
        direction=MessageLog.Direction.OUTBOUND,
        status=MessageLog.Status.SENT,  # simulating a missed sync (would normally be forced to FAILED)
        timestamp=timezone.now(),
    )
    recipient.message.message_log = log
    recipient.message.status = OutboundMessage.Status.FAILED
    recipient.message.save(update_fields=["message_log", "status"])

    stats = campaign_delivery_stats(campaign)
    assert stats["failed"] == 1
    assert stats["sent"] == 0


def _make_conversation(wa_contact):
    from apps.whatsapp.models import Conversation

    return Conversation.objects.create(account=wa_contact.account, contact=wa_contact)
