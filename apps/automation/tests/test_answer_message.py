"""A message-received workflow only keeps AI out when it actually answered the customer.

Regression: a "Welcome new customers" workflow enrolls on every message; for someone already
welcomed it goes straight to stop, and that silent run used to count as "handled", so AI never
replied to that customer again.
"""

from unittest import mock

import pytest

from apps.accounts.models import Account
from apps.automation.models import Workflow
from apps.automation.workflow_engine import answer_message
from apps.contacts import tags as contact_tags
from apps.contacts.models import Contact

WELCOME = {
    "trigger": {"type": "conversation.message_received"},
    "steps": [
        {
            "id": "new",
            "type": "branch",
            "field": "tag",
            "operator": "ne",
            "value": "welcomed",
            "on_true": "reply",
            "on_false": "stop",
        },
        {"id": "reply", "type": "reply_text", "text": "Hi!", "next": "tag"},
        {"id": "tag", "type": "add_tag", "tag": "welcomed", "next": "stop"},
        {"id": "stop", "type": "stop"},
    ],
}


@pytest.fixture
def contact(db):
    account = Account.objects.create(company_name="TaskCentro")
    Workflow.objects.create(
        account=account,
        name="Welcome new customers",
        slug="welcome-new-customers",
        status=Workflow.Status.PUBLISHED,
        definition=WELCOME,
    )
    return Contact.objects.create(account=account, phone="+260971234567")


def _answer(contact):
    with mock.patch(
        "apps.automation.workflow_engine._run_reply_text", return_value={}
    ) as send:
        answered = answer_message(
            contact.account_id, contact, context={"message": {"body": "Hi"}}
        )
    return answered, send.call_count


def test_first_message_is_answered_by_the_welcome(contact):
    assert _answer(contact) == (True, 1)


def test_already_welcomed_customer_is_left_for_ai(contact):
    contact_tags.add_tag(contact, "welcomed")
    assert _answer(contact) == (False, 0)


def test_welcome_only_once(contact):
    assert _answer(contact) == (True, 1)
    assert _answer(contact) == (False, 0)
