from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact, ContactEvent, ContactList, Segment
from apps.contacts.segments import SegmentError, contacts_for, count_for
from apps.contacts.services import import_csv, record_contact_event, upsert_contact


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
