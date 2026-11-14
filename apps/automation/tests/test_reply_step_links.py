"""A reply step links the outbound record of the channel it actually went out on.

``WorkflowStepRun`` has one foreign key per channel: ``outbound_message`` (WhatsApp),
``ig_outbound_message`` (Instagram) and ``fb_outbound_message`` (Messenger). Each channel's
failure hook (``apps.automation.integrations``) finds the step through its own key and fails
the run. The Instagram and Messenger replies used to report the *conversation message* id as
``outbound_message_id``, which the engine stored in the WhatsApp key: a broken foreign key, and
an unrelated WhatsApp send that failed with the same id would fail this run.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun
from apps.automation.workflow_engine import enroll
from apps.conversations.models import Conversation, Message

pytestmark = pytest.mark.django_db

REPLY = {
    "trigger": {"type": "conversation.message_received"},
    "steps": [
        {"id": "r", "type": "reply_text", "text": "Thanks, on it!", "next": "stop"},
        {"id": "stop", "type": "stop"},
    ],
}


def _run_reply(account, contact, conversation) -> WorkflowStepRun:
    wf = Workflow.objects.create(
        account=account,
        name="Reply",
        slug="reply",
        status=Workflow.Status.PUBLISHED,
        definition=REPLY,
    )
    run = enroll(
        wf,
        contact,
        context={"conversation_id": conversation.public_id, "message": {"body": "hi"}},
    )
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED, run.step_runs.values("result")
    return run.step_runs.get(step_id="r")


def test_messenger_reply_links_its_facebook_outbound():
    from apps.facebook.models import FacebookOutboundMessage
    from apps.facebook.tests.helpers import FakeGraph, make_messenger_contact, make_page

    account, _user, conn = make_page()
    fb_contact, _convo, spine = make_messenger_contact(conn)
    with patch(
        "apps.facebook.providers.meta.requests.request", side_effect=FakeGraph()
    ):
        step = _run_reply(account, fb_contact.contact, spine)

    outbound = FacebookOutboundMessage.objects.get(body="Thanks, on it!")
    assert step.outbound_message_id is None
    assert step.fb_outbound_message_id == outbound.pk
    message = Message.objects.get(pk=step.result["message_id"])
    assert message.conversation_id == spine.pk and message.body == "Thanks, on it!"


def test_instagram_reply_links_its_instagram_outbound():
    from apps.contacts.models import Contact
    from apps.instagram.models import OutboundMessage
    from apps.instagram.models.account import InstagramBusinessAccount
    from apps.instagram.models.contact import InstagramContact
    from apps.instagram.models.conversation import InstagramConversation
    from apps.instagram.providers.base import SendResult
    from apps.instagram.tests.helpers import make_account

    account, _user = make_account()
    ig_account = InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id="IBA-LINK",
        page_id="PAGE-LINK",
        access_token="token",
        verify_token="v",
        is_active=True,
    )
    contact = Contact.objects.create(account=account, source="instagram")
    ig_contact = InstagramContact.objects.create(
        account=account, instagram_scoped_id="IGSID-LINK", contact=contact
    )
    ig_convo = InstagramConversation.objects.create(
        instagram_account=ig_account,
        instagram_contact=ig_contact,
        last_inbound_at=timezone.now() - timedelta(minutes=5),
    )
    spine = Conversation.get_or_create_for_instagram(ig_convo)
    with patch(
        "apps.instagram.providers.meta.MetaInstagramProvider.send_message",
        return_value=SendResult(success=True, provider_message_id="mid.link"),
    ):
        step = _run_reply(account, contact, spine)

    outbound = OutboundMessage.objects.get(body="Thanks, on it!")
    assert step.outbound_message_id is None
    assert step.ig_outbound_message_id == outbound.pk
    assert Message.objects.get(pk=step.result["message_id"]).conversation_id == spine.pk


def test_a_failed_whatsapp_send_never_fails_a_messenger_run():
    """The visible symptom: WhatsApp's failure hook matched the Messenger step by id."""
    from apps.automation.integrations.whatsapp import mark_outbound_message_failed
    from apps.facebook.tests.helpers import FakeGraph, make_messenger_contact, make_page

    account, _user, conn = make_page()
    fb_contact, _convo, spine = make_messenger_contact(conn)
    with patch(
        "apps.facebook.providers.meta.requests.request", side_effect=FakeGraph()
    ):
        step = _run_reply(account, fb_contact.contact, spine)

    class FailedWhatsApp:  # any WhatsApp outbound whose id equals the conversation message's
        id = step.result["message_id"]
        last_error = "rejected"

    mark_outbound_message_failed(FailedWhatsApp())
    step.refresh_from_db()
    step.run.refresh_from_db()
    assert step.status == "ok"
    assert step.run.status == WorkflowRun.Status.COMPLETED
