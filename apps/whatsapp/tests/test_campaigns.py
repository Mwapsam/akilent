"""R1.5c: WhatsApp campaigns — the WhatsApp half of the channel-neutral
"Campaigns" concept. Deliberately MVP-sized; see apps.whatsapp.models.campaign."""
import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact, ContactList
from apps.whatsapp.campaigns import CampaignError, create_and_queue_campaign
from apps.whatsapp.models import MessageTemplate, OutboundMessage, WhatsAppCampaign, WhatsAppContact


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


@pytest.fixture
def approved_template(account):
    return MessageTemplate.objects.create(
        account=account, name="Promo", whatsapp_template_name="promo",
        content="Hi {{1}}, check out our sale!", variables=["name"],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )


@pytest.fixture
def contact_list_with_two_customers(account):
    contact_list = ContactList.objects.create(account=account, name="VIPs")
    for i in range(2):
        contact = Contact.objects.create(account=account, phone=f"+26097100000{i}")
        WhatsAppContact.objects.create(
            account=account, phone_number=contact.phone, contact=contact,
            opt_in_status=WhatsAppContact.OptInStatus.OPTED_IN,
        )
        contact_list.contacts.add(contact)
    return contact_list


@pytest.mark.django_db
def test_create_and_send_campaign_queues_one_outbound_message_per_recipient(
    account, approved_template, contact_list_with_two_customers, django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        campaign = create_and_queue_campaign(
            account=account, name="September promo", contact_list=contact_list_with_two_customers,
            template_id=approved_template.id,
        )

    campaign.refresh_from_db()
    assert campaign.status == WhatsAppCampaign.Status.COMPLETED
    assert campaign.recipient_count == 2
    assert campaign.queued_count == 2
    assert campaign.skipped_count == 0
    assert OutboundMessage.objects.filter(account=account, template=approved_template).count() == 2


@pytest.mark.django_db
def test_campaign_skips_opted_out_and_missing_whatsapp_contacts(
    account, approved_template, django_capture_on_commit_callbacks,
):
    contact_list = ContactList.objects.create(account=account, name="Everyone")
    opted_out_contact = Contact.objects.create(account=account, phone="+260971000010")
    WhatsAppContact.objects.create(
        account=account, phone_number=opted_out_contact.phone, contact=opted_out_contact,
        opt_in_status=WhatsAppContact.OptInStatus.OPTED_OUT,
    )
    no_whatsapp_contact = Contact.objects.create(account=account, phone="+260971000011")  # no WhatsAppContact row
    contact_list.contacts.add(opted_out_contact, no_whatsapp_contact)

    with django_capture_on_commit_callbacks(execute=True):
        campaign = create_and_queue_campaign(
            account=account, name="Blast", contact_list=contact_list, template_id=approved_template.id,
        )

    campaign.refresh_from_db()
    assert campaign.queued_count == 0
    assert campaign.skipped_count == 2
    assert not OutboundMessage.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_create_campaign_rejects_unapproved_template(account, contact_list_with_two_customers):
    draft = MessageTemplate.objects.create(
        account=account, name="Draft", whatsapp_template_name="draft", content="x",
        approval_status=MessageTemplate.ApprovalStatus.DRAFT,
    )
    with pytest.raises(CampaignError, match="approved"):
        create_and_queue_campaign(
            account=account, name="X", contact_list=contact_list_with_two_customers, template_id=draft.id,
        )
    assert not WhatsAppCampaign.objects.exists()


@pytest.mark.django_db
def test_create_campaign_rejects_empty_list(account, approved_template):
    empty_list = ContactList.objects.create(account=account, name="Empty")
    with pytest.raises(CampaignError, match="empty"):
        create_and_queue_campaign(
            account=account, name="X", contact_list=empty_list, template_id=approved_template.id,
        )


@pytest.mark.django_db
def test_variable_mapping_resolves_contact_attributes(
    account, approved_template, django_capture_on_commit_callbacks,
):
    contact_list = ContactList.objects.create(account=account, name="List")
    contact = Contact.objects.create(
        account=account, phone="+260971000020", attributes={"first_name": "Ada"},
    )
    WhatsAppContact.objects.create(
        account=account, phone_number=contact.phone, contact=contact,
        opt_in_status=WhatsAppContact.OptInStatus.OPTED_IN,
    )
    contact_list.contacts.add(contact)

    with django_capture_on_commit_callbacks(execute=True):
        create_and_queue_campaign(
            account=account, name="X", contact_list=contact_list, template_id=approved_template.id,
            variable_mapping={"name": "contact.first_name"},
        )

    msg = OutboundMessage.objects.get(account=account)
    assert msg.payload["params"] == {"name": "Ada"}
