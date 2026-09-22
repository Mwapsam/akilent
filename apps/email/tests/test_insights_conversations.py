"""R1.5b: Insights leads with a "Conversations" section (reusing the same
derived state the inbox uses) before any email-specific content."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND
NOW = timezone.now()


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _convo(account, *messages):
    contact = Contact.objects.create(account=account, phone=f"+2609700{Conversation.objects.count():05d}")
    c = Conversation.objects.create(account=account, contact=contact, channel="whatsapp")
    for direction, minutes_ago, status in messages:
        Message.objects.create(
            account=account, conversation=c, direction=direction, body="x",
            timestamp=NOW - timedelta(minutes=minutes_ago), status=status,
        )
    return c


@pytest.mark.django_db
def test_insights_shows_conversations_section_before_email(logged_in):
    client, account = logged_in
    _convo(account, (IN, 30, "delivered"))  # waiting for a reply

    resp = client.get("/email/insights/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Conversations" in body
    assert ">1<" in body  # the waiting count
    assert "customer waiting for you" in body  # singular, not "customers"
    # Conversations section renders before the Email section, not after.
    assert body.index("Conversations") < body.index("<h2 class=\"text-lg font-semibold text-ink pb-2\">Email</h2>")


@pytest.mark.django_db
def test_insights_conversations_scoped_to_account(logged_in):
    client, account = logged_in
    other = Account.objects.create(company_name="Other Co")
    _convo(other, (IN, 30, "delivered"))

    body = client.get("/email/insights/").content.decode()
    assert ">0<" in body
    assert "customers waiting for you" in body  # plural for zero
