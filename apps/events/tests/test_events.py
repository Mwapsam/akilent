from datetime import timezone as _tz

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact, ContactEvent
from apps.core.events import BusinessEventReceived, dispatcher
from apps.events.models import BusinessEvent
from apps.events.services import ingest_event


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


@pytest.mark.django_db
def test_ingest_creates_contact_by_email_and_links(account):
    ev = ingest_event(account, name="invoice.paid", customer="buyer@x.com",
                      data={"amount": 2500})
    assert ev.public_id.startswith("bev_")
    contact = Contact.objects.get(account=account, email="buyer@x.com")
    assert ev.contact_id == contact.id
    # activity appended
    assert ContactEvent.objects.filter(contact=contact, type="invoice.paid").exists()


@pytest.mark.django_db
def test_ingest_resolves_existing_contact_by_public_id(account):
    c = Contact.objects.create(account=account, email="known@x.com")
    ev = ingest_event(account, name="order.shipped", customer=c.public_id)
    assert ev.contact_id == c.id


@pytest.mark.django_db
def test_ingest_publishes_domain_event(account):
    received = []
    dispatcher.subscribe(BusinessEventReceived, received.append)
    ingest_event(account, name="signup.completed", customer="new@x.com", data={"plan": "pro"})
    assert len(received) == 1
    assert received[0].name == "signup.completed"
    assert received[0].data == {"plan": "pro"}


@pytest.mark.django_db
def test_ingest_without_customer_stores_unlinked_event(account):
    ev = ingest_event(account, name="system.maintenance")
    assert ev.contact_id is None
    assert BusinessEvent.objects.filter(account=account, name="system.maintenance").count() == 1


@pytest.mark.django_db
def test_ingest_requires_name(account):
    with pytest.raises(ValueError):
        ingest_event(account, name="")
