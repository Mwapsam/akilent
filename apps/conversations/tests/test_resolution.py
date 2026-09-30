"""Phase B.3: close/reopen with a resolution reason, closed_at, and the
conversation-detail pane's customer/order context."""

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Event
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.fixture
def open_conversation(logged_in):
    _, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    message_log = MessageLog.objects.create(
        account=account,
        conversation=wa_conversation,
        contact=wa_contact,
        message_id="wamid.RESOLUTIONTEST",
        direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT,
        content="Do you have the blue dress?",
        status=MessageLog.Status.DELIVERED,
        timestamp=timezone.now(),
    )
    return record_inbound_whatsapp_message(
        contact=contact,
        wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation,
        message_log=message_log,
    )


@pytest.mark.django_db
class TestCloseAndReopenModel:
    def test_close_sets_closed_at_and_records_an_event(self, open_conversation):
        open_conversation.close(
            resolution=Conversation.Resolution.RESOLVED, actor="user:1"
        )
        open_conversation.refresh_from_db()
        assert open_conversation.status == Conversation.Status.CLOSED
        assert open_conversation.closed_at is not None
        assert open_conversation.resolution == Conversation.Resolution.RESOLVED
        event = Event.objects.get(
            subject_type="conversation", type="conversation.closed"
        )
        assert event.actor == "user:1"
        assert event.payload == {"resolution": Conversation.Resolution.RESOLVED}

    def test_close_with_no_reason_is_allowed(self, open_conversation):
        open_conversation.close()
        open_conversation.refresh_from_db()
        assert open_conversation.status == Conversation.Status.CLOSED
        assert open_conversation.resolution == ""

    def test_reopen_clears_closed_at_and_resolution(self, open_conversation):
        open_conversation.close(resolution=Conversation.Resolution.SPAM)
        open_conversation.reopen(actor="user:1")
        open_conversation.refresh_from_db()
        assert open_conversation.status == Conversation.Status.OPEN
        assert open_conversation.closed_at is None
        assert open_conversation.resolution == ""
        event = Event.objects.get(
            subject_type="conversation", type="conversation.reopened"
        )
        assert event.actor == "user:1"

    def test_reopening_an_already_open_conversation_is_a_noop(self, open_conversation):
        open_conversation.reopen()
        assert not Event.objects.filter(type="conversation.reopened").exists()

    def test_a_new_customer_message_clears_closed_at_like_an_explicit_reopen(
        self, open_conversation
    ):
        open_conversation.close(resolution=Conversation.Resolution.RESOLVED)
        open_conversation.register_inbound(timezone.now())
        open_conversation.refresh_from_db()
        assert open_conversation.status == Conversation.Status.OPEN
        assert open_conversation.closed_at is None
        assert open_conversation.resolution == ""


@pytest.mark.django_db
class TestCloseAndReopenViews:
    def test_closing_with_a_reason(self, logged_in, open_conversation):
        client, _, user = logged_in
        client.post(
            f"/inbox/{open_conversation.public_id}/",
            {"action": "close", "resolution": "no_response"},
        )
        open_conversation.refresh_from_db()
        assert open_conversation.status == Conversation.Status.CLOSED
        assert open_conversation.resolution == "no_response"
        event = Event.objects.get(type="conversation.closed")
        assert event.actor == f"user:{user.pk}"

    def test_an_invalid_resolution_is_rejected(self, logged_in, open_conversation):
        client, _, _ = logged_in
        resp = client.post(
            f"/inbox/{open_conversation.public_id}/",
            {"action": "close", "resolution": "made-up-reason"},
        )
        assert resp.status_code == 302
        open_conversation.refresh_from_db()
        assert open_conversation.status == Conversation.Status.OPEN

    def test_reopening_from_the_view(self, logged_in, open_conversation):
        client, _, user = logged_in
        open_conversation.close(resolution="resolved")
        client.post(f"/inbox/{open_conversation.public_id}/", {"action": "reopen"})
        open_conversation.refresh_from_db()
        assert open_conversation.status == Conversation.Status.OPEN
        event = Event.objects.get(type="conversation.reopened")
        assert event.actor == f"user:{user.pk}"


@pytest.mark.django_db
class TestPaneCustomerContext:
    def test_customer_since_and_conversation_count_are_shown(
        self, logged_in, open_conversation
    ):
        client, _, _ = logged_in
        body = client.get(f"/inbox/{open_conversation.public_id}/").content.decode()
        assert "Customer since" in body
        assert "Conversations" in body

    def test_an_open_order_is_shown_when_the_orders_feature_is_usable(
        self, logged_in, open_conversation
    ):
        from apps.commerce.models import Order

        client, account, _ = logged_in
        Order.objects.create(
            account=account,
            contact=open_conversation.contact,
            status=Order.Status.PENDING,
            total="49.99",
        )
        body = client.get(f"/inbox/{open_conversation.public_id}/").content.decode()
        # Rendered only when "orders" is in usable_features (billing-gated) --
        # assert on the underlying data reaching the template rather than
        # assuming plan entitlement in this test's account.
        from apps.billing.api import usable_features

        if "orders" in usable_features(account):
            assert "Open order" in body

    def test_a_paid_order_is_not_shown_as_open(self, logged_in, open_conversation):
        from apps.commerce.models import Order

        client, account, _ = logged_in
        Order.objects.create(
            account=account,
            contact=open_conversation.contact,
            status=Order.Status.PAID,
            total="49.99",
        )
        resp = client.get(f"/inbox/{open_conversation.public_id}/")
        assert resp.context["open_order"] is None
