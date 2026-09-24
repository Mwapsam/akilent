"""An agent's template send must land in the conversation they sent it from.

Regression cover: sending a template is precisely what an agent does once a
conversation has lapsed, and `Conversation.get_or_open` opens a *new* session
row for a closed conversation — so the reply surfaced in a different inbox
thread than the one on screen.
"""
import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.automation.workflows import send_whatsapp_message
from apps.whatsapp.models import Conversation, MessageTemplate, WhatsAppContact
from apps.whatsapp.tasks import _ensure_outbound_log


@pytest.fixture
def setup(db):
    account = Account.objects.create(company_name="Co", slug="co")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567",
    )
    template = MessageTemplate.objects.create(
        account=account, name="Follow up", whatsapp_template_name="follow_up",
        language_code="en", content="Hi {{1}}", variables=["Customer name"],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    # A lapsed conversation: closed, and its 24h window long gone.
    lapsed = Conversation.objects.create(
        account=account, contact=wa_contact, is_open=False,
        closed_at=timezone.now(), window_expires_at=timezone.now() - timezone.timedelta(days=3),
    )
    return account, wa_contact, template, lapsed


@pytest.mark.django_db
def test_pinned_send_stays_in_the_lapsed_conversation(setup):
    account, wa_contact, template, lapsed = setup

    msg = send_whatsapp_message(
        account, phone=wa_contact.phone_number, template_id=template.id,
        params={"Customer name": "Ada"}, conversation=lapsed,
    )
    log = _ensure_outbound_log(msg)

    assert log.conversation_id == lapsed.id
    assert Conversation.objects.filter(contact=wa_contact).count() == 1


@pytest.mark.django_db
def test_unpinned_send_still_opens_a_new_session(setup):
    """Workflow/campaign sends carry no conversation and keep the old behaviour."""
    account, wa_contact, template, lapsed = setup

    msg = send_whatsapp_message(
        account, phone=wa_contact.phone_number, template_id=template.id,
        params={"Customer name": "Ada"},
    )
    log = _ensure_outbound_log(msg)

    assert log.conversation_id != lapsed.id


@pytest.mark.django_db
def test_recipient_comes_from_the_conversations_whatsapp_identity(setup):
    """The canonical Contact's phone can drift from the number the conversation
    actually runs on (an updated mobile, say). Replying must use the identity
    that owns the conversation rather than the Contact's current phone, which
    may match no WhatsApp record at all."""
    from apps.contacts.models import Contact
    from apps.conversations.actions import run_action
    from apps.conversations.models import Conversation as SpineConversation
    from apps.whatsapp.models import OutboundMessage

    account, wa_contact, template, lapsed = setup
    person = Contact.objects.create(account=account, phone="+260979999999")
    wa_contact.contact = person
    wa_contact.save(update_fields=["contact"])
    spine = SpineConversation.objects.create(
        account=account, contact=person, channel=SpineConversation.Channel.WHATSAPP,
        whatsapp_conversation=lapsed,
    )

    run_action(
        "send_whatsapp", {"account": account}, account=account,
        phone=person.phone, template_id=template.id,
        params={"Customer name": "Ada"}, conversation=spine,
    )

    msg = OutboundMessage.objects.get(account=account)
    assert msg.contact_id == wa_contact.id


@pytest.mark.django_db
def test_pin_to_another_contacts_conversation_is_ignored(setup):
    account, wa_contact, template, lapsed = setup
    other_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260970000000",
    )
    other_convo = Conversation.objects.create(account=account, contact=other_contact)

    msg = send_whatsapp_message(
        account, phone=wa_contact.phone_number, template_id=template.id,
        params={"Customer name": "Ada"}, conversation=other_convo,
    )
    log = _ensure_outbound_log(msg)

    assert log.conversation_id != other_convo.id
    assert log.conversation.contact_id == wa_contact.id
