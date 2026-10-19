"""Every customer with a phone gets a WhatsApp identity, so campaigns can reach
them — not only people who happened to message the business first."""

import pytest
from django.core.management import call_command

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.whatsapp.models import WhatsAppContact
from apps.whatsapp.services.contacts import ensure_whatsapp_identity


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


def test_creates_unknown_opt_in_identity(account):
    contact = Contact.objects.create(account=account, phone="+260971111111")
    WhatsAppContact.objects.filter(contact=contact).delete()

    identity = ensure_whatsapp_identity(contact)

    assert identity.contact_id == contact.pk
    assert identity.phone_number == "+260971111111"
    assert identity.opt_in_status == WhatsAppContact.OptInStatus.UNKNOWN


def test_links_existing_unlinked_identity_without_duplicating(account):
    stray = WhatsAppContact.objects.create(
        account=account, phone_number="+260971111112"
    )
    contact = Contact.objects.create(account=account, phone="+260971111112")

    identity = ensure_whatsapp_identity(contact)

    assert identity.pk == stray.pk
    assert identity.contact_id == contact.pk
    assert WhatsAppContact.objects.filter(account=account).count() == 1


def test_never_steals_an_identity_linked_to_another_customer(account):
    owner = Contact.objects.create(account=account, email="owner@example.com")
    WhatsAppContact.objects.create(
        account=account, phone_number="+260971111113", contact=owner
    )
    other = Contact.objects.create(account=account, phone="+260971111113")

    identity = ensure_whatsapp_identity(other)

    assert identity.contact_id == owner.pk


def test_no_phone_or_invalid_phone_returns_none(account):
    assert ensure_whatsapp_identity(Contact(account=account, email="a@b.co")) is None
    assert ensure_whatsapp_identity(Contact(account=account, phone="12")) is None


def test_is_scoped_to_the_contacts_account(account):
    other_account = Account.objects.create(company_name="Other")
    WhatsAppContact.objects.create(account=other_account, phone_number="+260971111114")
    contact = Contact.objects.create(account=account, phone="+260971111114")

    identity = ensure_whatsapp_identity(contact)

    assert identity.account_id == account.pk
    assert WhatsAppContact.objects.filter(phone_number="+260971111114").count() == 2


def test_saving_a_customer_with_a_phone_creates_identity_after_commit(
    account, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        contact = Contact.objects.create(account=account, phone="+260971111115")

    assert WhatsAppContact.objects.filter(account=account, contact=contact).exists()


def test_changing_phone_adds_identity_for_the_new_number(
    account, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        contact = Contact.objects.create(account=account, phone="+260971111116")
    with django_capture_on_commit_callbacks(execute=True):
        contact.phone = "+260971111117"
        contact.save(update_fields=["phone", "updated_at"])

    phones = set(
        WhatsAppContact.objects.filter(contact=contact).values_list(
            "phone_number", flat=True
        )
    )
    assert "+260971111117" in phones


def test_unrelated_saves_do_not_touch_identities(
    account, django_capture_on_commit_callbacks
):
    contact = Contact.objects.create(account=account, phone="+260971111118")
    WhatsAppContact.objects.filter(contact=contact).delete()
    with django_capture_on_commit_callbacks(execute=True):
        contact.first_name = "Ada"
        contact.save(update_fields=["first_name", "updated_at"])

    assert not WhatsAppContact.objects.filter(contact=contact).exists()


def test_backfill_command_is_idempotent_and_dry_run_changes_nothing(account, capsys):
    contacts = [
        Contact.objects.create(account=account, phone=f"+26097122222{i}")
        for i in range(3)
    ]
    WhatsAppContact.objects.filter(contact__in=contacts).delete()

    call_command("backfill_whatsapp_identities", "--dry-run")
    assert "3 customers" in capsys.readouterr().out
    assert not WhatsAppContact.objects.filter(contact__in=contacts).exists()

    call_command("backfill_whatsapp_identities")
    call_command("backfill_whatsapp_identities")
    assert WhatsAppContact.objects.filter(contact__in=contacts).count() == 3
