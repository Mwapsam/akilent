"""Plans include features; operators make exceptions; owners switch optional tools off.

Covers the catalog rules, the access() precedence table, the migration that carried today's
access over, the Operator Console matrix and business exceptions, the locked page and pricing.
"""
import importlib
import re
from decimal import Decimal
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing import api as billing_api
from apps.billing import features as catalog
from apps.billing.models import (
    AccountFeatureOverride, ComingSoonFeature, ModuleSubscription, Plan, PlanFeature, Subscription,
)
from apps.core.models import AdminAction

APPS = Path(__file__).resolve().parents[2]


# ---- the catalog --------------------------------------------------------------------------------

def test_keys_are_permanent():
    """Every issued key is still in the catalog (retire with DEPRECATED, never delete), and every
    catalog key was added to ISSUED_KEYS."""
    assert set(catalog.BY_KEY) == set(catalog.ISSUED_KEYS)


def test_every_key_the_code_checks_exists():
    pattern = re.compile(r'(?:module_required|usable|entitled|set_owner_switch)\(\s*(?:[\w.]+,\s*)?"([a-z_]+)"')
    used = set()
    for path in APPS.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        used |= set(pattern.findall(path.read_text(encoding="utf-8-sig")))
    used.discard("not-a-feature")
    unknown = used - set(catalog.BY_KEY)
    assert not unknown, f"checked but not in the catalog: {unknown}"


def test_core_features_are_never_in_the_matrix_or_overridable():
    matrix = {f.key for f in catalog.matrix_features()}
    for key in catalog.core_keys():
        assert key not in matrix and not catalog.overridable(key) and not catalog.plan_assignable(key)


# ---- entitlement --------------------------------------------------------------------------------

@pytest.fixture
def plan(db):
    p = Plan.objects.create(slug="starter-x", name="Starter X", price_monthly=Decimal("10"))
    PlanFeature.objects.filter(plan=p).delete()  # start empty: this test decides what's in it
    return p


@pytest.fixture
def account(db, plan):
    acc = Account.objects.create(company_name="Acme", slug="acme")
    Subscription.objects.update_or_create(account=acc, defaults={
        "plan": plan, "status": Subscription.ACTIVE, "current_period_start": timezone.now()})
    return acc


def _set(account, plan, *, in_plan=False, override=None, owner_off=False, key="sales"):
    if in_plan:
        PlanFeature.objects.create(plan=plan, key=key)
    if override is not None:
        AccountFeatureOverride.objects.create(account=account, key=key, grant=override, note="n")
    if owner_off:
        billing_api.set_owner_switch(account, key, False)


@pytest.mark.django_db
@pytest.mark.parametrize("in_plan,override,owner_off,entitled,usable,source", [
    (True, None, False, True, True, "plan"),
    (True, None, True, True, False, "plan"),          # owner off never changes the source
    (False, None, False, False, False, "not_in_plan"),
    (False, True, False, True, True, "grant"),
    (True, True, False, True, True, "plan"),          # a redundant grant: the plan is the source
    (True, False, False, False, False, "removed"),    # a removal beats the plan
    (False, False, False, False, False, "removed"),
])
def test_access_precedence(account, plan, in_plan, override, owner_off, entitled, usable, source):
    _set(account, plan, in_plan=in_plan, override=override, owner_off=owner_off)
    info = billing_api.access(account, "sales")
    assert (info["entitled"], info["usable"], info["source"]) == (entitled, usable, source)
    assert info["owner_off"] is owner_off
    assert (info["override"] is not None) is (override is not None)


@pytest.mark.django_db
def test_core_is_always_on_and_cant_be_removed(account):
    assert billing_api.entitled(account, "inbox") and billing_api.entitled(account, "whatsapp")
    assert billing_api.access(account, "inbox")["source"] == "core"
    with pytest.raises(billing_api.FeatureError):
        billing_api.set_override(account, "inbox", grant=False, note="no")


@pytest.mark.django_db
def test_an_override_needs_a_note(account):
    with pytest.raises(billing_api.FeatureError):
        billing_api.set_override(account, "sales", grant=True, note="  ")


@pytest.mark.django_db
def test_not_sold_is_grant_only_and_never_offers_an_upgrade(account, plan, monkeypatch):
    hidden = catalog.Feature("sales", "Sales tracking", "p", "Sales", availability=catalog.NOT_SOLD, optional=True)
    monkeypatch.setitem(catalog.BY_KEY, "sales", hidden)
    monkeypatch.setattr(catalog, "FEATURES", tuple(hidden if f.key == "sales" else f for f in catalog.FEATURES))
    PlanFeature.objects.create(plan=plan, key="sales")  # a stale tick doesn't sell it
    assert billing_api.entitled(account, "sales") is False
    assert billing_api.access(account, "sales")["available_in"] == []
    assert "sales" not in {f.key for f in catalog.matrix_features()}
    billing_api.set_override(account, "sales", grant=True, note="pilot")
    assert billing_api.access(account, "sales")["source"] == "grant"


@pytest.mark.django_db
def test_deprecated_is_never_entitled(account, plan, monkeypatch):
    gone = catalog.Feature("insights", "Insights", "p", "Email", availability=catalog.DEPRECATED)
    monkeypatch.setitem(catalog.BY_KEY, "insights", gone)
    PlanFeature.objects.create(plan=plan, key="insights")
    AccountFeatureOverride.objects.create(account=account, key="insights", grant=True, note="n")
    assert billing_api.entitled(account, "insights") is False
    assert billing_api.access(account, "insights")["source"] == "retired"


@pytest.mark.django_db
def test_coming_soon_never_gives_access(account, plan):
    item = ComingSoonFeature.objects.create(name="Instagram")
    item.plans.add(plan)
    assert billing_api.entitled_features(account) == catalog.core_keys()


@pytest.mark.django_db
def test_a_business_with_no_subscription_keeps_the_old_module_features(db):
    acc = Account.objects.create(company_name="Bare", slug="bare")
    Subscription.objects.filter(account=acc).delete()
    assert billing_api.entitled(acc, "automations") and billing_api.entitled(acc, "ai_assistant")
    assert billing_api.access(acc, "automations")["source"] == "no_plan"
    assert not billing_api.entitled(acc, "email_campaigns")


# ---- the migration: today's access, represented, nothing taken away ----------------------------

@pytest.mark.django_db
def test_migration_keeps_todays_access(db):
    migration = importlib.import_module("apps.billing.migrations.0020_seed_plan_features")
    old = Plan.objects.create(slug="old", name="Old", bulk_email=True, email_apis=True, detailed_analytics=False)
    acc = Account.objects.create(company_name="Blocked", slug="blocked")
    ModuleSubscription.objects.create(account=acc, module="ai", enabled=False)
    ModuleSubscription.objects.create(account=acc, module="whatsapp", enabled=False)
    ModuleSubscription.objects.create(account=acc, module="crm", enabled=False)
    PlanFeature.objects.all().delete()
    Plan.objects.filter(slug="pilot").delete()

    migration.forwards(django_apps, None)

    keys = set(PlanFeature.objects.filter(plan=old).values_list("key", flat=True))
    assert {"email_campaigns", "email_sending", "automations", "ai_assistant", "sales", "orders",
            "whatsapp_campaigns", "verification_codes"} <= keys
    assert "insights" not in keys
    removed = set(AccountFeatureOverride.objects.filter(account=acc, grant=False).values_list("key", flat=True))
    assert removed == {"ai_assistant", "whatsapp_campaigns", "verification_codes"}
    assert billing_api.owner_switched_off(acc, "sales"), "the owner's own switch stays a switch"
    pilot = Plan.objects.get(slug="pilot")
    assert not pilot.is_active
    assert {f.key for f in catalog.matrix_features()} <= set(pilot.features.values_list("key", flat=True))


# ---- Operator Console ---------------------------------------------------------------------------

@pytest.fixture
def op_client(client, db):
    client.force_login(User.objects.create_superuser("root", "root@example.com", "pw"))
    return client


@pytest.mark.django_db
def test_matrix_preview_changes_nothing_then_apply_changes_and_audits(op_client, account, plan):
    PlanFeature.objects.create(plan=plan, key="sales")
    form = {f"f:{plan.pk}:ai_assistant": "on"}  # add AI, drop sales
    page = op_client.post("/manage/plans/features/", form).content.decode()
    assert "Confirm these changes" in page and "1 business" in page
    assert set(plan.features.values_list("key", flat=True)) == {"sales"}, "a preview must not write"

    op_client.post("/manage/plans/features/", dict(form, apply="1"))
    assert set(plan.features.values_list("key", flat=True)) == {"ai_assistant"}
    added = AdminAction.objects.get(action="plan.feature.add")
    assert added.detail["key"] == "ai_assistant" and added.detail["businesses"] == 1
    assert added.detail["old"] is False and added.detail["new"] is True
    assert AdminAction.objects.filter(action="plan.feature.remove", detail__key="sales").exists()


@pytest.mark.django_db
def test_matrix_is_for_operators_only(client, account, plan):
    owner = User.objects.create_user("o", "o@x.com", "pw")
    Membership.objects.create(user=owner, account=account, role=Membership.Role.OWNER)
    client.force_login(owner)
    assert client.get("/manage/plans/features/").status_code == 403
    client.post("/manage/plans/features/", {f"f:{plan.pk}:ai_assistant": "on", "apply": "1"})
    assert not plan.features.exists()


@pytest.mark.django_db
def test_business_page_grant_remove_reset(op_client, account, plan):
    url = f"/manage/businesses/{account.pk}/do/feature/"
    op_client.post(url, {"key": "sales", "change": "grant", "note": "pilot deal"})
    assert billing_api.access(account, "sales")["source"] == "grant"
    row = AdminAction.objects.get(action="business.feature.grant")
    assert row.detail["old_source"] == "not_in_plan" and row.detail["note"] == "pilot deal"

    op_client.post(url, {"key": "sales", "change": "remove", "note": "unpaid"})
    assert billing_api.access(account, "sales")["source"] == "removed"
    op_client.post(url, {"key": "sales", "change": "reset"})
    assert billing_api.access(account, "sales")["source"] == "not_in_plan"
    assert AdminAction.objects.filter(action="business.feature.reset").exists()

    op_client.post(url, {"key": "inbox", "change": "remove", "note": "x"})
    assert not AccountFeatureOverride.objects.filter(key="inbox").exists()

    page = op_client.get(f"/manage/businesses/{account.pk}/tab/billing/").content.decode()
    assert "Sales tracking" in page and "Not included" in page


# ---- what the business sees ---------------------------------------------------------------------

@pytest.fixture
def owner_client(client, account):
    owner = User.objects.create_user("owner", "owner@x.com", "pw")
    Membership.objects.create(user=owner, account=account, role=Membership.Role.OWNER)
    client.force_login(owner)
    return client


@pytest.mark.django_db
def test_locked_page_says_why_and_where_to_get_it(owner_client, account, plan):
    pro = Plan.objects.create(slug="pro-x", name="Pro X", price_monthly=Decimal("40"))
    assert "sales" in set(pro.features.values_list("key", flat=True))  # seeded like other plans

    resp = owner_client.get("/sales/")
    assert resp.status_code == 302 and resp.url == "/billing/locked/sales/"
    page = owner_client.get(resp.url).content.decode()
    assert "Not included in your Starter X plan" in page and "Pro X" in page and "View plans" in page

    billing_api.set_override(account, "sales", grant=False, note="x")
    page = owner_client.get("/billing/locked/sales/").content.decode()
    assert "turned off for your business by an account administrator" in page and "View plans" not in page


@pytest.mark.django_db
def test_owner_switched_off_points_to_settings(owner_client, account, plan):
    PlanFeature.objects.create(plan=plan, key="orders")
    billing_api.set_owner_switch(account, "orders", False)
    page = owner_client.get("/billing/locked/orders/").content.decode()
    assert "You switched this off" in page


@pytest.mark.django_db
def test_nav_shows_a_lock_for_a_feature_not_in_the_plan(owner_client, account, plan):
    page = owner_client.get("/inbox/").content.decode()
    assert 'href="/billing/locked/automations/"' in page and "data-locked" in page
    PlanFeature.objects.create(plan=plan, key="automations")
    assert 'href="/billing/locked/automations/"' not in owner_client.get("/inbox/").content.decode()


@pytest.mark.django_db
def test_optional_tools_only_lists_what_the_plan_includes(owner_client, account, plan):
    page = owner_client.get("/settings/tools/").content.decode()
    assert 'name="sales"' not in page
    PlanFeature.objects.create(plan=plan, key="sales")
    assert 'name="sales"' in owner_client.get("/settings/tools/").content.decode()


@pytest.mark.django_db
def test_pricing_is_built_from_the_catalog(owner_client, plan):
    PlanFeature.objects.create(plan=plan, key="email_campaigns")
    ComingSoonFeature.objects.create(name="Instagram conversations").plans.add(plan)
    page = owner_client.get("/billing/plans/").content.decode()
    assert "Email campaigns" in page and "Instagram conversations" in page and "Coming soon" in page
    assert "Compare plans" in page and "Included in every plan" in page
    assert "mailbox" not in page.lower() and "forwarding rule" not in page


@pytest.mark.django_db
def test_signup_bullets_match_the_pricing_card(plan):
    from apps.accounts.views import _plan_feature_bullets

    PlanFeature.objects.create(plan=plan, key="insights")
    card = billing_api.plan_card(plan, whatsapp=False)
    assert _plan_feature_bullets(plan) == card["limits"] + ["Detailed insights"]


# ---- WhatsApp: templates are core, campaigns and codes are sold --------------------------------

@pytest.mark.django_db
@override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled", WHATSAPP_ENABLED=True)
def test_templates_work_without_campaigns_or_codes(owner_client, account):
    assert owner_client.get("/whatsapp/templates/new/").status_code == 200
    resp = owner_client.get("/whatsapp/campaigns/new/")
    assert resp.status_code == 302 and resp.url == "/billing/locked/whatsapp_campaigns/"
