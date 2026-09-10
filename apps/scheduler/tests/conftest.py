from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailDomain


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(
        slug="p", name="P", price_monthly=Decimal("10"),
        max_emails_per_month=1000, email_apis=True, email_templates=True,
        bulk_email=True, api_rate_per_min=0, max_bulk_recipients_per_campaign=-1,
    )
    Subscription.objects.create(
        account=acc, plan=plan, status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )
    return acc


@pytest.fixture
def verified_domain(account):
    return EmailDomain.objects.create(
        account=account, domain="mail.acme.com", status=EmailDomain.Status.VERIFIED,
    )


@pytest.fixture
def future():
    return lambda **kw: timezone.now() + timedelta(**kw)
