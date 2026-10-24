"""seed_demo_workspace builds the sales-demo journey, rebuilds cleanly, and never touches real accounts."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import override_settings

from apps.accounts.management.commands.seed_demo_workspace import DEMO_SLUG
from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost
from apps.publishing.results import results_for


def _seed():
    call_command("seed_demo_workspace", password="pw-demo-123")


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_builds_the_journey_with_post_attribution():
    _seed()
    account = Account.objects.get(slug=DEMO_SLUG)

    rows = [
        r
        for post in ContentPost.objects.filter(account=account, status="published")
        for r in results_for(post)
    ]
    assert len(rows) == 3  # dress on Instagram and Facebook, sandals on Instagram
    assert all(r["chats"] == 1 and r["leads"] == 1 and r["orders"] == 1 for r in rows)
    paid = sorted(x["total"] for r in rows for x in r["revenue"])
    assert paid == [Decimal("45"), Decimal("60")]
    # Nothing in the workspace can reach Meta: no ready Instagram account, no metric polling.
    assert not account.instagram_accounts.filter(is_active=True).exists()
    assert not account.whatsapp_numbers.exists()


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_running_twice_rebuilds_one_workspace():
    _seed()
    _seed()
    assert Account.objects.filter(slug=DEMO_SLUG).count() == 1
    account = Account.objects.get(slug=DEMO_SLUG)
    assert ContentPost.objects.filter(account=account).count() == 3


@pytest.mark.django_db
@override_settings(DEBUG=False)
def test_refuses_without_debug():
    with pytest.raises(SystemExit):
        _seed()
    assert not Account.objects.filter(slug=DEMO_SLUG).exists()


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_never_deletes_an_account_that_took_the_slug():
    user = User.objects.create_user("real@example.com", "real@example.com", "pw")
    account = Account.objects.create(company_name="Real Shop", slug=DEMO_SLUG)
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)

    with pytest.raises(SystemExit):
        _seed()
    assert Account.objects.filter(pk=account.pk, company_name="Real Shop").exists()
