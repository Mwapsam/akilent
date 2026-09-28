"""Public API for the billing module: the one place that decides what a business can use.

Three separate questions, asked in this order:
- ``entitled(account, key)``: commercial access. The plan includes it, or an operator granted it,
  and no operator removed it. Core features are always entitled.
- ``usable(account, key)``: entitled, and the owner hasn't switched the optional tool off.
- (limits, later) whether one operation may use another unit. An exhausted quota never makes a
  feature look disabled.

Application code asks these functions and nothing else: it never reads a Plan column or
``ModuleSubscription`` (``apps/core/tests/test_entitlement_boundary.py`` enforces this), so
changing what a plan includes is configuration in the Operator Console, not a deploy.

Direct imports of apps.billing.models are not allowed outside of billing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction

from apps.accounts.models import Account
from apps.billing import features as catalog
from apps.billing import limit_catalog

# Unit economics live in costs.py; re-exported for the Operator Console.
from apps.billing.costs import (
    DRIVERS as COST_DRIVERS,
)

# Limits and usage live in metering.py; re-exported here because other apps only import billing.api.
from apps.billing.metering import (  # noqa: F401
    LimitReached,
    check_rule,
    commit,
    count,
    limit,
    limit_source,
    release,
    require_room,
    reserve,
    reserve_all,
    reserve_all_verbose,
    settle_operation,
    usage_report,
    used,
)
from apps.billing.models import (
    AccountFeatureOverride,
    ComingSoonFeature,
    ModuleSubscription,
    Plan,
    PlanFeature,
    Subscription,
    UsageSummary,
)

logger = logging.getLogger(__name__)

# source values for access()
SOURCE_CORE = "core"
SOURCE_PLAN = "plan"
SOURCE_GRANT = "grant"
SOURCE_REMOVED = "removed"
SOURCE_NOT_IN_PLAN = "not_in_plan"
SOURCE_RETIRED = "retired"
SOURCE_NO_PLAN = (
    "no_plan"  # no subscription row at all: keeps the pre-plan module features
)


class FeatureError(ValueError):
    """A feature change that isn't allowed (unknown key, core feature, not optional, ...)."""


@dataclass
class _State:
    plan: Plan | None
    has_subscription: bool
    plan_keys: frozenset
    overrides: dict = field(default_factory=dict)
    owner_off: frozenset = frozenset()


def _state(account: Account) -> _State:
    """Everything entitlement depends on for one business, read fresh (a handful of small
    queries). Not cached on the account object: the same object often outlives a change."""
    sub = Subscription.objects.filter(account=account).select_related("plan").first()
    plan = sub.plan if sub else None
    plan_keys = (
        frozenset(PlanFeature.objects.filter(plan=plan).values_list("key", flat=True))
        if plan
        else frozenset()
    )
    overrides = {
        o.key: o
        for o in AccountFeatureOverride.objects.filter(account=account).select_related(
            "set_by"
        )
    }
    off_modules = set(
        ModuleSubscription.objects.filter(
            account=account,
            enabled=False,
            module__in=list(catalog.OWNER_SWITCH_MODULE.values()),
        ).values_list("module", flat=True)
    )
    return _State(
        plan=plan,
        has_subscription=sub is not None,
        plan_keys=plan_keys,
        overrides=overrides,
        owner_off=frozenset(
            k for k, m in catalog.OWNER_SWITCH_MODULE.items() if m in off_modules
        ),
    )


def _decide(state: _State, feature: catalog.Feature) -> tuple[bool, str]:
    """(entitled, source) for one feature. A removal beats plan and grant; a grant is the source
    only when the plan doesn't include the feature."""
    if feature.availability == catalog.DEPRECATED:
        return False, SOURCE_RETIRED
    if feature.access_mode == catalog.CORE:
        return True, SOURCE_CORE
    override = (
        state.overrides.get(feature.key)
        if feature.availability != catalog.INTERNAL
        else None
    )
    if override is not None and not override.grant:
        return False, SOURCE_REMOVED
    if not state.has_subscription and feature.key in catalog.MODULE_FEATURES:
        return True, SOURCE_NO_PLAN
    if feature.key in state.plan_keys and feature.availability == catalog.SELLABLE:
        return True, SOURCE_PLAN
    if override is not None and override.grant:
        return True, SOURCE_GRANT
    return False, SOURCE_NOT_IN_PLAN


def entitled(account: Account, key: str) -> bool:
    """Does the plan (or an operator exception) give this business ``key``?

    Raises KeyError for a key that isn't in the catalog, so a typo fails loudly.
    """
    feature = catalog.get(key)
    if account is None:
        return False
    return _decide(_state(account), feature)[0]


def usable(account: Account, key: str) -> bool:
    """Entitled, and the owner hasn't switched this optional tool off."""
    if not entitled(account, key):
        return False
    return key not in _state(account).owner_off


def entitled_features(account: Account) -> frozenset[str]:
    """Every catalog key this business is entitled to."""
    state = _state(account)
    return frozenset(f.key for f in catalog.FEATURES if _decide(state, f)[0])


def usable_features(account: Account) -> frozenset[str]:
    """``entitled_features`` minus the optional tools the owner switched off."""
    state = _state(account)
    return (
        frozenset(f.key for f in catalog.FEATURES if _decide(state, f)[0])
        - state.owner_off
    )


def nav_state(account: Account) -> dict:
    """What every page's nav needs, from one read: ``locked`` (not entitled), ``tools_off``
    (the owner switched them off) and ``usable``."""
    state = _state(account)
    entitled_keys = frozenset(f.key for f in catalog.FEATURES if _decide(state, f)[0])
    return {
        "locked": frozenset(catalog.BY_KEY) - entitled_keys,
        "tools_off": state.owner_off,
        "usable": entitled_keys - state.owner_off,
    }


def available_in(key: str) -> list[str]:
    """Names of the active plans a business could move to for ``key``, cheapest first. Empty for
    a feature that isn't sold (so the locked page never offers an upgrade that doesn't exist)."""
    feature = catalog.get(key)
    if feature.availability != catalog.SELLABLE or feature.access_mode != catalog.PLAN:
        return []
    return list(
        Plan.objects.filter(is_active=True, features__key=key)
        .order_by("price_monthly")
        .values_list("name", flat=True)
    )


def access(account: Account, key: str) -> dict:
    """Everything the locked page and the Business 360 view show for one feature."""
    feature = catalog.get(key)
    state = _state(account)
    is_entitled, source = _decide(state, feature)
    override = state.overrides.get(key)
    owner_off = key in state.owner_off
    return {
        "key": key,
        "name": feature.name,
        "pitch": feature.pitch,
        "group": feature.group,
        "optional": feature.optional,
        "entitled": is_entitled,
        "usable": is_entitled and not owner_off,
        "source": source,
        "plan_name": state.plan.name if state.plan else None,
        "owner_off": owner_off,
        "override": None
        if override is None
        else {
            "grant": override.grant,
            "note": override.note,
            "set_by": override.set_by.get_username() if override.set_by else "",
            "created": override.updated_at or override.created_at,
        },
        "available_in": available_in(key) if not is_entitled else [],
        "overridable": catalog.overridable(key),
    }


def access_report(account: Account) -> list[dict]:
    """``access`` for every shown catalog feature, in catalog order (Business 360)."""
    return [
        access(account, f.key)
        for f in catalog.FEATURES
        if f.availability not in (catalog.DEPRECATED, catalog.INTERNAL)
    ]


# ---- owner switches (Settings > Optional tools) -----------------------------------------------


def set_owner_switch(account: Account, key: str, on: bool) -> None:
    """Turn an optional tool on or off for this business: the owner's choice, not commercial
    access. Raises FeatureError for a feature that isn't optional."""
    module = catalog.OWNER_SWITCH_MODULE.get(key)
    if module is None:
        raise FeatureError(f"{key!r} isn't an optional tool.")
    ModuleSubscription.objects.update_or_create(
        account=account, module=module, defaults={"enabled": on}
    )


def owner_switched_off(account: Account, key: str) -> bool:
    return key in _state(account).owner_off


# ---- operator changes ---------------------------------------------------------------------------


def set_override(
    account: Account, key: str, *, grant: bool, note: str, by=None
) -> dict:
    """Grant or remove ``key`` for one business. Returns the access state before the change,
    for the audit log. Raises FeatureError for a core, internal or unknown feature."""
    if not catalog.overridable(key):
        raise FeatureError("That feature can't be granted or removed for one business.")
    note = (note or "").strip()
    if not note:
        raise FeatureError("Add a note saying why.")
    before = access(account, key)
    AccountFeatureOverride.objects.update_or_create(
        account=account,
        key=key,
        defaults={"grant": grant, "note": note[:255], "set_by": by},
    )
    return before


def clear_override(account: Account, key: str) -> dict:
    """Remove the operator exception, so the plan decides again. Returns the state before."""
    catalog.get(key)
    before = access(account, key)
    AccountFeatureOverride.objects.filter(account=account, key=key).delete()
    return before


def plan_feature_matrix() -> dict:
    """{plan_id: set(keys)} for every plan, for the Operator Console matrix."""
    matrix: dict = {p.pk: set() for p in Plan.objects.all()}
    for plan_id, key in PlanFeature.objects.values_list("plan_id", "key"):
        matrix.setdefault(plan_id, set()).add(key)
    return matrix


def diff_plan_features(wanted: dict) -> list[dict]:
    """What applying ``wanted`` ({plan_id: set(keys)}) would change, per plan, with how many
    businesses are on that plan. Only matrix features are compared; nothing is written."""
    current = plan_feature_matrix()
    assignable = {f.key for f in catalog.matrix_features()}
    plans = {p.pk: p for p in Plan.objects.all()}
    changes = []
    for plan_id, keys in wanted.items():
        if plan_id not in plans:
            continue
        have = current.get(plan_id, set()) & assignable
        want = set(keys) & assignable
        added, removed = sorted(want - have), sorted(have - want)
        if added or removed:
            changes.append(
                {
                    "plan": plans[plan_id],
                    "added": added,
                    "removed": removed,
                    "businesses": Subscription.objects.filter(plan_id=plan_id).count(),
                }
            )
    return changes


@transaction.atomic
def apply_plan_features(wanted: dict) -> list[dict]:
    """Apply the matrix. Returns the same change list as ``diff_plan_features`` (for the audit
    log). Features outside the matrix (not sold, internal) are left alone."""
    changes = diff_plan_features(wanted)
    for change in changes:
        plan = change["plan"]
        for key in change["added"]:
            PlanFeature.objects.get_or_create(plan=plan, key=key)
        PlanFeature.objects.filter(plan=plan, key__in=change["removed"]).delete()
    return changes


# ---- limits and usage (see apps.billing.metering for the reservation contract) ------------------


def whatsapp_limit_for(category: str, *, verification_code: bool = False) -> str:
    """Which monthly limit a WhatsApp template send of ``category`` uses."""
    if verification_code:
        return "verification_codes_month"
    return limit_catalog.TEMPLATE_CATEGORY_LIMIT.get(
        (category or "").lower(), "whatsapp_utility_msgs"
    )


def count_conversation(account: Account) -> None:
    """A customer opened a new 24h conversation window. Counted, never blocked (SOFT)."""
    count(account, "conversations_month")


def plan_limit_matrix() -> dict:
    """{plan_id: {key: value}} for every plan, for the Operator Console limits page."""
    from apps.billing.models import PlanLimit

    matrix: dict = {p.pk: {} for p in Plan.objects.all()}
    for plan_id, key, value in PlanLimit.objects.values_list("plan_id", "key", "value"):
        matrix.setdefault(plan_id, {})[key] = value
    return matrix


def diff_plan_limits(wanted: dict) -> list[dict]:
    """What applying ``wanted`` ({plan_id: {key: value}}) would change, per plan, with how many
    businesses are on it. Nothing is written."""
    current = plan_limit_matrix()
    plans = {p.pk: p for p in Plan.objects.all()}
    changes = []
    for plan_id, values in wanted.items():
        if plan_id not in plans:
            continue
        rows = []
        for key, value in values.items():
            if key not in limit_catalog.BY_KEY:
                continue
            old = current.get(plan_id, {}).get(key, limit_catalog.get(key).default)
            if old != value:
                rows.append(
                    {
                        "key": key,
                        "name": limit_catalog.get(key).name,
                        "old": old,
                        "new": value,
                    }
                )
        if rows:
            changes.append(
                {
                    "plan": plans[plan_id],
                    "limits": rows,
                    "businesses": Subscription.objects.filter(plan_id=plan_id).count(),
                }
            )
    return changes


@transaction.atomic
def apply_plan_limits(wanted: dict) -> list[dict]:
    from apps.billing.models import PlanLimit

    changes = diff_plan_limits(wanted)
    for change in changes:
        for row in change["limits"]:
            PlanLimit.objects.update_or_create(
                plan=change["plan"], key=row["key"], defaults={"value": row["new"]}
            )
    return changes


def save_cost_settings(
    *, unit_costs_by_driver: dict, fixed, target_margin_pct: int
) -> None:
    """Set what Akilent pays per unit (email, AI action), the fixed cost per business and the
    target margin."""
    from apps.billing.models import CostSettings, UnitCost

    for driver, value in unit_costs_by_driver.items():
        if driver in {d.key for d in COST_DRIVERS}:
            UnitCost.objects.update_or_create(
                driver=driver, defaults={"cost_per_unit": value}
            )
    settings = CostSettings.load()
    settings.fixed_monthly_cost_per_business = fixed
    settings.target_margin_pct = max(0, min(100, int(target_margin_pct)))
    settings.save()


def cost_settings():
    from apps.billing.models import CostSettings

    return CostSettings.load()


def set_limit_override(
    account: Account, key: str, *, value: int, note: str, by=None, expires_at=None
) -> dict:
    """Give one business a different limit than its plan. Returns the state before (for audit)."""
    from apps.billing.models import AccountLimitOverride

    limit_catalog.get(key)
    note = (note or "").strip()
    if not note:
        raise FeatureError("Add a note saying why.")
    if value < -1:
        raise FeatureError("Use -1 for unlimited, or 0 or more.")
    old, source = limit_source(account, key)
    AccountLimitOverride.objects.update_or_create(
        account=account,
        key=key,
        defaults={
            "value": value,
            "note": note[:255],
            "set_by": by,
            "expires_at": expires_at,
        },
    )
    return {"value": old, "source": source}


def clear_limit_override(account: Account, key: str) -> dict:
    from apps.billing.models import AccountLimitOverride

    old, source = limit_source(account, key)
    AccountLimitOverride.objects.filter(account=account, key=key).delete()
    return {"value": old, "source": source}


# ---- pricing ------------------------------------------------------------------------------------


def _limit_line(value: int, unit: str) -> str:
    if value == 1 and unit.endswith("s"):
        unit = unit[:-1]
    return f"Unlimited {unit}" if value == -1 else f"{value:,} {unit}"


# (limit key, pricing label, WhatsApp only, shown even when unlimited). Others show only if capped.
_PRICING_LIMITS = (
    ("whatsapp_numbers", "WhatsApp numbers", True, True),
    ("conversations_month", "conversations/mo", True, True),
    ("whatsapp_marketing_msgs", "WhatsApp marketing messages/mo", True, False),
    ("whatsapp_utility_msgs", "WhatsApp utility messages/mo", True, False),
    ("verification_codes_month", "verification codes/mo", True, False),
    ("emails_month", "emails/mo", False, True),
    ("emails_day", "emails/day", False, False),
    ("ai_actions_day", "AI actions/day", False, False),
    ("automation_rules", "active automations", False, False),
    ("contacts", "customers", False, False),
    ("team_members", "team members", False, False),
)


def plan_card(plan: Plan, *, whatsapp: bool = True) -> dict:
    """What a pricing card says about ``plan``: its limits, the features it includes in catalog
    order, and coming-soon teasers. The pricing page, signup wizard and landing page all use it,
    so what's advertised is what the plan gives."""
    from apps.billing.models import PlanLimit

    keys = set(PlanFeature.objects.filter(plan=plan).values_list("key", flat=True))
    values = dict(PlanLimit.objects.filter(plan=plan).values_list("key", "value"))
    limits = []
    for key, label, whatsapp_only, always in _PRICING_LIMITS:
        if whatsapp_only and not whatsapp:
            continue
        value = values.get(key, limit_catalog.get(key).default)
        if always or value != limit_catalog.UNLIMITED:
            limits.append(_limit_line(value, label))
    if plan.log_retention_days:
        limits.append(f"{plan.log_retention_days} days of message history")
    included = [
        f
        for f in catalog.matrix_features()
        if f.key in keys
        and (whatsapp or f.key not in ("whatsapp_campaigns", "verification_codes"))
    ]
    return {
        "plan": plan,
        "limits": limits,
        "features": included,
        "feature_names": [f.name for f in included],
        "coming_soon": list(plan.coming_soon.filter(is_active=True)),
    }


def default_signup_trial() -> tuple[Plan | None, int]:
    """The plan and trial length a plain signup (no ``?plan=``) starts on.

    The auto_create_trial signal uses this, and the landing page advertises its number, so what
    the page promises is what signup gives: the site's default plan, else the trial plan, with
    that plan's trial_days falling back to the site default. ``(None, 0)`` when there's no plan.
    """
    from apps.core.models import SiteSettings

    site = SiteSettings.load()
    plan = site.default_plan or Plan.objects.filter(slug=Plan.TRIAL).first()
    if plan is None:
        return None, 0
    return plan, plan.trial_days or site.default_trial_days


def core_features() -> list:
    """What every plan includes (for "Included in every plan" on pricing)."""
    return [
        f
        for f in catalog.FEATURES
        if f.access_mode == catalog.CORE and f.availability != catalog.DEPRECATED
    ]


def compare_plans(plans, *, whatsapp: bool = True) -> list[dict]:
    """Rows for a features x plans comparison table: [{feature, cells: [bool per plan]}]."""
    plans = list(plans)
    matrix = plan_feature_matrix()
    rows = []
    for f in catalog.matrix_features():
        if not whatsapp and f.key in ("whatsapp_campaigns", "verification_codes"):
            continue
        rows.append(
            {"feature": f, "cells": [f.key in matrix.get(p.pk, set()) for p in plans]}
        )
    return rows


def coming_soon_features():
    return ComingSoonFeature.objects.prefetch_related("plans")


# ---- subscription and usage ---------------------------------------------------------------------


def get_subscription(account: Account) -> Subscription | None:
    """Get the subscription for an account.

    Returns:
        Subscription instance or None if no subscription exists
    """
    return Subscription.objects.filter(account=account).first()


def get_plan(account: Account) -> Plan | None:
    """Get the plan for an account.

    Returns:
        Plan instance or None if no subscription/plan exists
    """
    subscription = Subscription.objects.filter(account=account).first()
    return subscription.plan if subscription else None


def get_current_usage(account: Account) -> UsageSummary | None:
    """Get current usage for an account."""
    return UsageSummary.objects.filter(account=account).first()


def get_email_usage(account: Account) -> int:
    """Get total email usage for an account (emails sent this billing period)."""
    return UsageSummary.get_current_email_usage(account)
