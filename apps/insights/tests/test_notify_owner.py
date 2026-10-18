"""notify_owner emails the owners, once per waiting customer per window."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.insights import executor


def _convo(pk):
    return SimpleNamespace(pk=pk, contact_id=pk)


@pytest.fixture
def policy(db):
    from apps.accounts.models import Account
    from apps.insights.models import BusinessPolicy

    account = Account.objects.create(company_name="Samantha Store", slug="sam")
    return BusinessPolicy.objects.create(
        account=account,
        name="Unanswered DMs",
        trigger=BusinessPolicy.Trigger.CONVERSATION_UNANSWERED,
        condition={"hours": 2},
        action={"type": "notify_owner"},
        status=BusinessPolicy.Status.ACTIVE,
    )


def _run(policy, targets):
    with (
        patch.object(executor, "_check_trigger", return_value=targets),
        patch("apps.accounts.notifications.notify_team", return_value=1) as notify,
    ):
        outcome = executor._run_policy(policy, policy.account)
    return outcome, notify


@pytest.mark.django_db
def test_owners_are_emailed_with_a_link_to_the_inbox(policy):
    outcome, notify = _run(policy, [_convo(1), _convo(2)])
    assert outcome == "executed"
    kwargs = notify.call_args.kwargs
    assert kwargs["to"] == "owners"
    assert "2 conversations" in kwargs["subject"]
    from django.urls import reverse

    assert kwargs["path"] == reverse("conversations:inbox")


@pytest.mark.django_db
def test_the_same_waiting_customers_are_not_emailed_again(policy):
    _run(policy, [_convo(1), _convo(2)])
    outcome, notify = _run(policy, [_convo(1), _convo(2)])
    assert outcome == "skipped"
    notify.assert_not_called()


@pytest.mark.django_db
def test_a_new_waiting_customer_triggers_a_new_email(policy):
    _run(policy, [_convo(1)])
    outcome, notify = _run(policy, [_convo(1), _convo(3)])
    assert outcome == "executed"
    notify.assert_called_once()
