"""The "campaign" policy action only claims a send that will actually happen."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.insights import executor


@pytest.fixture
def setup(db):
    from apps.accounts.models import Account
    from apps.contacts.models import ContactList
    from apps.insights.models import BusinessPolicy
    from apps.whatsapp.models import MessageTemplate, WhatsAppCampaign

    account = Account.objects.create(company_name="Samantha Store", slug="sam")
    template = MessageTemplate.objects.create(
        account=account,
        name="Promo",
        whatsapp_template_name="promo",
        content="Hi",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    clist = ContactList.objects.create(account=account, name="VIPs")
    campaign = WhatsAppCampaign.objects.create(
        account=account,
        name="Win-back",
        contact_list=clist,
        template=template,
        status=WhatsAppCampaign.Status.COMPLETED,
    )
    policy = BusinessPolicy.objects.create(
        account=account,
        name="Inactive customers",
        trigger=BusinessPolicy.Trigger.CUSTOMER_INACTIVE,
        condition={"days": 30},
        action={"type": "campaign", "campaign_id": campaign.pk},
        status=BusinessPolicy.Status.ACTIVE,
    )
    return SimpleNamespace(account=account, campaign=campaign, policy=policy)


def _run(policy):
    with (
        patch.object(executor, "_check_trigger", return_value=[SimpleNamespace(pk=1)]),
        patch("apps.whatsapp.campaigns.send_campaign.delay") as delay,
    ):
        outcome = executor._run_policy(policy, policy.account)
    return outcome, delay


@pytest.mark.django_db
def test_an_already_sent_campaign_is_recorded_as_failed_not_queued(setup):
    from apps.insights.models import PolicyExecution

    outcome, delay = _run(setup.policy)

    assert outcome == "failed"
    delay.assert_not_called()
    pe = PolicyExecution.objects.get(policy=setup.policy)
    assert "can't be sent again" in pe.error


@pytest.mark.django_db
def test_a_queued_campaign_is_dispatched(setup):
    from apps.whatsapp.models import WhatsAppCampaign

    setup.campaign.status = WhatsAppCampaign.Status.QUEUED
    setup.campaign.save(update_fields=["status"])

    outcome, delay = _run(setup.policy)

    assert outcome == "executed"
    delay.assert_called_once_with(setup.campaign.pk)


@pytest.mark.django_db
def test_another_accounts_campaign_is_refused(setup):
    from apps.accounts.models import Account

    other = Account.objects.create(company_name="Other", slug="other")
    setup.policy.account = other
    setup.policy.save(update_fields=["account"])

    outcome, delay = _run(setup.policy)

    assert outcome == "failed"
    delay.assert_not_called()
