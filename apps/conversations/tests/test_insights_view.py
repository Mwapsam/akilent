"""The Insights (Business Health) page at /insights/: renders before Email, and is scoped to
the account. See apps.conversations.insights_views and apps.conversations.reporting."""

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
    contact = Contact.objects.create(
        account=account, phone=f"+2609700{Conversation.objects.count():05d}"
    )
    c = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    for direction, minutes_ago, status in messages:
        Message.objects.create(
            account=account,
            conversation=c,
            direction=direction,
            body="x",
            timestamp=NOW - timedelta(minutes=minutes_ago),
            status=status,
        )
    return c


@pytest.mark.django_db
def test_insights_renders_at_the_new_url(logged_in):
    client, account = logged_in
    resp = client.get("/insights/")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_opportunities_at_risk_renders_before_email(logged_in):
    client, account = logged_in
    _convo(account, (IN, 30, "delivered"))  # waiting for a reply

    body = client.get("/insights/").content.decode()
    assert "customer is waiting for a reply" in body
    # Conversation content renders before the Email section, not after.
    assert body.index("Opportunities at risk") < body.index(
        '<h2 id="email-h" class="text-lg font-semibold text-ink">Email</h2>'
    )


@pytest.mark.django_db
def test_insights_is_scoped_to_the_account(logged_in):
    client, account = logged_in
    other = Account.objects.create(company_name="Other Co")
    _convo(other, (IN, 30, "delivered"))

    body = client.get("/insights/").content.decode()
    # The other account's waiting customer must not leak; setup nudges (hours, automations)
    # still show for a fresh account with no data of its own, so the section isn't empty.
    assert "customer is waiting for a reply" not in body
    assert "0 customer" not in body
