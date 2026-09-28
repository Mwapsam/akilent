import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.billing.models import UsageSummary


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Quota Co")


@pytest.mark.django_db
def test_reserve_email_allows_under_cap(account):
    assert UsageSummary.reserve_email(account, cap=2) is True
    assert UsageSummary.get_current_email_usage(account) == 1


@pytest.mark.django_db
def test_reserve_email_blocks_at_cap(account):
    assert UsageSummary.reserve_email(account, cap=1) is True
    assert UsageSummary.reserve_email(account, cap=1) is False
    # The blocked reservation must not have incremented the counter.
    assert UsageSummary.get_current_email_usage(account) == 1


@pytest.mark.django_db
def test_reserve_email_unlimited_never_blocks(account):
    for _ in range(5):
        assert UsageSummary.reserve_email(account, cap=-1) is True
    assert UsageSummary.get_current_email_usage(account) == 5


@pytest.mark.django_db
def test_reserve_email_never_overshoots_cap_under_concurrent_calls(account):
    period_start = timezone.now().date().replace(day=1)
    UsageSummary.objects.create(
        account=account, period_start=period_start, emails_used=0
    )

    cap = 3
    results = [UsageSummary.reserve_email(account, cap=cap) for _ in range(10)]
    accepted = sum(1 for r in results if r)

    assert accepted == cap
    assert UsageSummary.get_current_email_usage(account) == cap


@pytest.mark.django_db
def test_release_email_refunds_the_count(account):
    UsageSummary.reserve_email(account, cap=5)
    UsageSummary.reserve_email(account, cap=5)
    assert UsageSummary.get_current_email_usage(account) == 2

    UsageSummary.release_email(account)
    assert UsageSummary.get_current_email_usage(account) == 1


@pytest.mark.django_db
def test_release_email_does_not_go_negative(account):
    UsageSummary.objects.create(
        account=account,
        period_start=timezone.now().date().replace(day=1),
        emails_used=0,
    )
    UsageSummary.release_email(account)
    assert UsageSummary.get_current_email_usage(account) == 0
