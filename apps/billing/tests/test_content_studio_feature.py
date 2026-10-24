"""Content Studio is a catalog feature included on every plan: existing plans (migration
0029), new plans (signals) and businesses without a subscription all agree, and the
normal entitlement checks (overrides, lapsed plans) gate its pages."""

import importlib

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing import api as billing_api
from apps.billing import features as catalog
from apps.billing.models import Plan, PlanFeature, Subscription

pytestmark = pytest.mark.django_db


def test_content_studio_is_a_sellable_plan_feature():
    feature = catalog.get("content_studio")
    assert feature.access_mode == catalog.PLAN
    assert feature.availability == catalog.SELLABLE
    assert "content_studio" in catalog.ISSUED_KEYS
    assert "content_studio" in {f.key for f in catalog.matrix_features()}


def test_a_new_plan_includes_it():
    plan = Plan.objects.create(slug="new", name="New", price_monthly=5)
    assert PlanFeature.objects.filter(plan=plan, key="content_studio").exists()


def test_the_migration_gives_every_existing_plan_the_feature_once():
    plan = Plan.objects.create(slug="old", name="Old", price_monthly=5)
    PlanFeature.objects.filter(plan=plan, key="content_studio").delete()
    migration = importlib.import_module(
        "apps.billing.migrations.0029_plans_include_content_studio"
    )
    migration.forwards(django_apps, None)
    migration.forwards(django_apps, None)  # running again changes nothing
    assert PlanFeature.objects.filter(plan=plan, key="content_studio").count() == 1


def test_no_subscription_keeps_it():
    account = Account.objects.create(company_name="No Plan")
    Subscription.objects.filter(account=account).delete()
    assert billing_api.usable(account, "content_studio")


@pytest.fixture
def owner(client):
    user = User.objects.create_user("o@example.com", "o@example.com", "pw")
    account = Account.objects.create(company_name="Studio Co")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    plan = Plan.objects.create(slug="paid", name="Paid", price_monthly=19)
    Subscription.objects.update_or_create(
        account=account,
        defaults={
            "plan": plan,
            "status": Subscription.ACTIVE,
            "current_period_start": timezone.now(),
        },
    )
    client.force_login(user)
    return account


def test_pages_open_on_a_plan_with_it(owner, client):
    assert client.get("/publishing/").status_code == 200


def test_an_operator_removal_locks_the_pages(owner, client):
    billing_api.set_override(owner, "content_studio", grant=False, note="test")
    resp = client.get("/publishing/new/")
    assert resp.status_code == 302
    assert resp.url == "/billing/locked/content_studio/"


def test_a_plan_without_it_locks_the_pages_and_the_nav(owner, client):
    PlanFeature.objects.filter(key="content_studio").delete()
    assert client.get("/publishing/").url == "/billing/locked/content_studio/"
    assert "content_studio" not in billing_api.nav_state(owner)["usable"]


def test_disconnecting_a_page_is_never_locked(owner, client):
    billing_api.set_override(owner, "content_studio", grant=False, note="test")
    resp = client.post("/publishing/fb/disconnect/")
    assert resp.url != "/billing/locked/content_studio/"
