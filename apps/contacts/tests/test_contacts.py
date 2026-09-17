from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact, ContactEvent, ContactList, Segment
from apps.contacts.segments import SegmentError, contacts_for, count_for
from apps.contacts.services import (
    import_csv,
    record_contact_event,
    upsert_contact,
    upsert_contact_by_phone,
)


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


@pytest.mark.django_db
def test_upsert_creates_then_updates(account):
    c1, created1 = upsert_contact(account, "A@Example.com", first_name="Al", attributes={"plan": "free"})
    assert created1 and c1.email == "a@example.com"
    c2, created2 = upsert_contact(account, "a@example.com", attributes={"country": "ZM"})
    assert not created2 and c2.pk == c1.pk
    assert c2.attributes == {"plan": "free", "country": "ZM"}
    assert Contact.objects.filter(account=account).count() == 1


@pytest.mark.django_db
def test_upsert_contact_by_phone_creates_phone_only_contact(account):
    c, created = upsert_contact_by_phone(account, "0971234567")
    assert created
    assert c.phone == "+260971234567"
    assert c.email is None


@pytest.mark.django_db
def test_upsert_contact_by_phone_is_idempotent(account):
    c1, created1 = upsert_contact_by_phone(account, "0971234567", attributes={"plan": "free"})
    c2, created2 = upsert_contact_by_phone(account, "+260971234567", attributes={"country": "ZM"})
    assert created1 and not created2
    assert c1.pk == c2.pk
    assert c2.attributes == {"plan": "free", "country": "ZM"}
    assert Contact.objects.filter(account=account).count() == 1


@pytest.mark.django_db
def test_upsert_contact_by_phone_does_not_collide_with_email_only_contact(account):
    upsert_contact(account, "a@x.com")
    phone_contact, created = upsert_contact_by_phone(account, "0971234567")
    assert created
    assert Contact.objects.filter(account=account).count() == 2
    assert phone_contact.email is None


@pytest.mark.django_db
def test_upsert_contact_by_phone_concurrent_create_yields_one_contact(account):
    """The partial unique constraint on (account, phone) — not application-level
    locking — must be what prevents a double-create when two requests race for
    the same unseen phone number."""
    from django.db import IntegrityError, transaction

    from apps.whatsapp.models.contact import normalize_phone

    normalized = normalize_phone("0971234567")

    # Simulate the race: both "requests" attempt a raw create for the same
    # normalized phone: the second must fail at the database level.
    Contact.objects.create(account=account, phone=normalized, email=None)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Contact.objects.create(account=account, phone=normalized, email=None)

    assert Contact.objects.filter(account=account, phone=normalized).count() == 1

    # get_or_create on top of that constraint must resolve cleanly to the one row.
    contact, created = upsert_contact_by_phone(account, "0971234567")
    assert not created
    assert Contact.objects.filter(account=account, phone=normalized).count() == 1


@pytest.mark.django_db
def test_import_csv_with_attribute_mapping(account):
    csv_text = "email,first,tier\na@x.com,Ada,gold\nb@x.com,Ben,silver\n,skip,me\n"
    imp = import_csv(account, csv_text, mapping={"email": "email", "first": "first_name", "tier": "attr:tier"})
    assert imp.row_count == 3
    assert imp.created_count == 2
    assert imp.skipped_count == 1
    ada = Contact.objects.get(account=account, email="a@x.com")
    assert ada.first_name == "Ada"
    assert ada.attributes["tier"] == "gold"


@pytest.mark.django_db
def test_import_csv_normalizes_phone_column(account):
    csv_text = "email,phone\na@x.com,0971234567\nb@x.com,not-a-number\n"
    import_csv(account, csv_text, mapping={"email": "email", "phone": "phone"})
    ada = Contact.objects.get(account=account, email="a@x.com")
    assert ada.phone == "+260971234567"
    # Malformed phone must not fail the row — stored as-is rather than blocking import.
    ben = Contact.objects.get(account=account, email="b@x.com")
    assert ben.phone == "not-a-number"


@pytest.mark.django_db
def test_record_event_updates_engagement_and_status(account):
    c, _ = upsert_contact(account, "u@x.com")
    record_contact_event(c, "email.opened")
    c.refresh_from_db()
    assert c.last_engaged_at is not None

    record_contact_event(c, "email.unsubscribed")
    c.refresh_from_db()
    assert c.status == Contact.Status.UNSUBSCRIBED


@pytest.mark.django_db
def test_segment_attribute_and_boolean_group(account):
    upsert_contact(account, "zm1@x.com", attributes={"country": "ZM", "tier": "premium"})
    upsert_contact(account, "zm2@x.com", attributes={"country": "ZM", "tier": "free"})
    upsert_contact(account, "us1@x.com", attributes={"country": "US", "tier": "premium"})

    definition = {
        "op": "and",
        "conditions": [
            {"field": "attributes.country", "operator": "eq", "value": "ZM"},
            {"field": "attributes.tier", "operator": "eq", "value": "premium"},
        ],
    }
    assert count_for(definition, account) == 1
    assert contacts_for(definition, account).first().email == "zm1@x.com"


@pytest.mark.django_db
def test_segment_last_engaged_and_opened_facts(account):
    old = upsert_contact(account, "cold@x.com")[0]
    old.last_engaged_at = timezone.now() - timedelta(days=120)
    old.save()
    warm = upsert_contact(account, "warm@x.com")[0]
    record_contact_event(warm, "email.opened")

    # not engaged in the last 90 days
    d = {"op": "and", "conditions": [{"field": "last_engaged_days", "operator": "gte", "value": 90}]}
    emails = {c.email for c in contacts_for(d, account)}
    assert emails == {"cold@x.com"}

    d2 = {"op": "and", "conditions": [{"field": "opened_in_last_90d", "operator": "eq", "value": True}]}
    assert {c.email for c in contacts_for(d2, account)} == {"warm@x.com"}


@pytest.mark.django_db
def test_segment_rejects_bad_field(account):
    with pytest.raises(SegmentError):
        count_for({"op": "and", "conditions": [{"field": "password", "operator": "eq", "value": "x"}]}, account)


@pytest.mark.django_db
def test_list_membership_segment(account):
    a = upsert_contact(account, "a@x.com")[0]
    upsert_contact(account, "b@x.com")
    lst = ContactList.objects.create(account=account, name="VIPs")
    lst.contacts.add(a)
    d = {"op": "and", "conditions": [{"field": "in_list", "operator": "eq", "value": "vips"}]}
    assert {c.email for c in contacts_for(d, account)} == {"a@x.com"}
