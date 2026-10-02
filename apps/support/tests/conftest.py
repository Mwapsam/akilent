import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account
from apps.support.models import SLAPolicy, SupportCategory, SupportQueue


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Test Co")


@pytest.fixture
def another_account(db):
    return Account.objects.create(company_name="Other Co")


@pytest.fixture
def agent(db):
    return User.objects.create_user(username="agent1", password="x")


@pytest.fixture
def payments_category(db):
    return SupportCategory.objects.create(slug="payments", name="Payments")


@pytest.fixture
def payments_queue(db):
    return SupportQueue.objects.create(slug="payments", name="Payments")


@pytest.fixture
def general_queue(db):
    return SupportQueue.objects.create(slug="general", name="General Support")


@pytest.fixture
def sla_t1_p1(db):
    return SLAPolicy.objects.create(
        customer_tier=1,
        priority="p1",
        first_response_minutes=480,
        resolution_minutes=1440,
    )


@pytest.fixture
def sla_t4_p1(db):
    return SLAPolicy.objects.create(
        customer_tier=4,
        priority="p1",
        first_response_minutes=30,
        resolution_minutes=240,
        escalation_after_minutes=15,
    )


@pytest.fixture
def sla_t3_p1(db):
    return SLAPolicy.objects.create(
        customer_tier=3,
        priority="p1",
        first_response_minutes=120,
        resolution_minutes=480,
        escalation_after_minutes=30,
    )
