"""BulkEmailCampaignVersion snapshot + rollback."""
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import BulkEmailCampaign, BulkEmailCampaignVersion, EmailDomain
from apps.email.services.bulk import create_campaign
from apps.email.services.campaign_versions import restore_campaign_version, snapshot_campaign


@pytest.fixture
def account(db):
    user = User.objects.create_user("o", "o@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    Plan.objects.create(slug="p", name="P", price_monthly=Decimal("1"))
    return acc


@pytest.fixture
def domain(account):
    return EmailDomain.objects.create(
        account=account, domain="mail.acme.com", status=EmailDomain.Status.VERIFIED,
    )


@pytest.mark.django_db
def test_create_campaign_takes_an_initial_snapshot(account, domain):
    # snapshot is synchronous inside create_campaign; the dispatch is on_commit
    campaign = create_campaign(
        account=account, from_email="hi@mail.acme.com",
        subject_override="Hello", text_override="body",
        recipients=[{"to": "a@x.com", "variables": {}}],
    )
    versions = list(campaign.versions.all())
    assert len(versions) == 1
    assert versions[0].number == 1
    assert versions[0].label == "submitted"
    assert versions[0].subject_override == "Hello"
    assert versions[0].recipient_count == 1


@pytest.mark.django_db
def test_snapshot_numbers_increment(account, domain):
    campaign = BulkEmailCampaign.objects.create(
        account=account, domain=domain, from_email="hi@mail.acme.com",
        subject_override="v1",
    )
    v1 = snapshot_campaign(campaign)
    campaign.subject_override = "v2"
    campaign.save(update_fields=["subject_override"])
    v2 = snapshot_campaign(campaign, label="edit")
    assert (v1.number, v2.number) == (1, 2)


@pytest.mark.django_db
def test_restore_is_draft_only(account, domain):
    campaign = BulkEmailCampaign.objects.create(
        account=account, domain=domain, from_email="hi@mail.acme.com",
        subject_override="original", status=BulkEmailCampaign.Status.DRAFT,
    )
    snapshot_campaign(campaign)
    campaign.subject_override = "changed"
    campaign.save(update_fields=["subject_override"])

    restore_campaign_version(campaign, 1)
    campaign.refresh_from_db()
    assert campaign.subject_override == "original"

    campaign.status = BulkEmailCampaign.Status.SENDING
    campaign.save(update_fields=["status"])
    with pytest.raises(ValueError):
        restore_campaign_version(campaign, 1)
