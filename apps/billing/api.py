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
from datetime import timedelta

from django.db import models, transaction

from apps.accounts.models import Account
from apps.billing import features as catalog
from apps.billing import limit_catalog

# Unit economics live in costs.py; re-exported for the Operator Console.
from apps.billing.costs import (
    DRIVERS as COST_DRIVERS,
)
from apps.billing.costs import (
    actual_cost,  # noqa: F401
    businesses_by_margin,  # noqa: F401
    loss_reasons,  # noqa: F401
    plan_economics,  # noqa: F401
    unit_costs,  # noqa: F401
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
from apps.billing.metering import (
    warnings as usage_warnings,  # noqa: F401
)
from apps.billing.models import (
    AccountFeatureOverride,
    ComingSoonFeature,
    ModuleSubscription,
    Plan,
    PlanFeature,
    Subscription,
    UsageReservation,  # noqa: F401
    UsageSummary,
)
from apps.core import request_context

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
# Subscription states whose plan gives no features (see _state).
LAPSED_STATUSES = frozenset(
    {Subscription.INCOMPLETE, Subscription.EXPIRED, Subscription.CANCELLED}
)


class FeatureError(ValueError):
    """A feature change that isn't allowed (unknown key, core feature, not optional, ...)."""


@dataclass
class _State:
    plan: Plan | None
    has_subscription: bool
    plan_keys: frozenset
    status: str = ""
    overrides: dict = field(default_factory=dict)
    owner_off: frozenset = frozenset()


def forget() -> None:
    """Drop what this request remembered of entitlement, after a change to plans, features,
    switches or a subscription (apps.billing.signals, and the writes below that skip signals)."""
    request_context.forget("billing")


def _state(account: Account) -> _State:
    """Everything entitlement depends on for one business (a handful of small queries), read
    once per page (``apps.core.request_context.remembered``; a page asks entitled()/usable()
    many times). Not cached on the account object: the same object often outlives a change."""
    if account.pk is None:
        return _read_state(account)
    return request_context.remembered(
        ("billing", account.pk), lambda: _read_state(account)
    )


def _read_state(account: Account) -> _State:
    sub = Subscription.objects.filter(account=account).select_related("plan").first()
    plan = sub.plan if sub else None
    # A plan only gives its features while it's paid for (or trialing, or past due within the
    # grace period). Unpaid, expired and cancelled businesses keep core features and anything an
    # operator granted; nothing they made is deleted.
    lapsed = (
        sub is not None
        and is_lapsed(sub)
        and not getattr(account, "is_platform_account", False)
    )
    plan_keys = (
        frozenset(PlanFeature.objects.filter(plan=plan).values_list("key", flat=True))
        if plan and not lapsed
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
        status=sub.status if sub else "",
        plan_keys=plan_keys,
        overrides=overrides,
        owner_off=frozenset(
            k for k, m in catalog.OWNER_SWITCH_MODULE.items() if m in off_modules
        ),
    )


def is_lapsed(sub, now=None) -> bool:
    """Does ``sub``'s plan give no features right now? Its status says so, or a date has
    passed that the hourly jobs haven't acted on yet: a trial past ``trial_ends_at``, or an
    unpaid renewal past its grace period. Access follows the date itself, so a late or
    stopped job never gives features away."""
    from django.utils import timezone

    if sub.status in LAPSED_STATUSES:
        return True
    now = now or timezone.now()
    if sub.status == Subscription.TRIALING:
        return bool(sub.trial_ends_at and sub.trial_ends_at <= now)
    if sub.status == Subscription.PAST_DUE and sub.past_due_since:
        from apps.billing.renewals import grace_period

        return sub.past_due_since + grace_period() <= now
    return False


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


def subscription_status(account: Account) -> str:
    """The subscription's status ("" when there is none), from the same read as entitlement."""
    return _state(account).status


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


def plan_channel_keys(plan: Plan, features=None) -> frozenset[str]:
    """The channels ``plan`` sells, whether or not they're on right now. ``features`` is the
    plan's feature keys when the caller already has them (e.g. from ``plan_feature_matrix``)."""
    from apps.core.channels import CHANNEL_FEATURE

    if features is None:
        features = set(
            PlanFeature.objects.filter(plan=plan).values_list("key", flat=True)
        )
    return frozenset(ch for ch, key in CHANNEL_FEATURE.items() if key in features)


@transaction.atomic
def set_plan_channels(plan: Plan, channels) -> dict:
    """Make ``plan`` sell exactly ``channels`` (channel keys), through their channel features.
    Returns ``{"added": [...], "removed": [...]}`` feature keys, for the audit log."""
    from apps.core.channels import CHANNEL_FEATURE

    want = {CHANNEL_FEATURE[ch] for ch in channels if ch in CHANNEL_FEATURE}
    have = set(
        PlanFeature.objects.filter(
            plan=plan, key__in=CHANNEL_FEATURE.values()
        ).values_list("key", flat=True)
    )
    added, removed = sorted(want - have), sorted(have - want)
    PlanFeature.objects.bulk_create(
        [PlanFeature(plan=plan, key=k) for k in added], ignore_conflicts=True
    )
    PlanFeature.objects.filter(plan=plan, key__in=removed).delete()
    forget()  # bulk_create sends no signal
    return {"added": added, "removed": removed}


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


# (limit key, pricing label, shown even when unlimited). Others show only if capped.
# Conversations (every channel) first, then each channel's own limits in channel order.
_PRICING_LIMITS = (
    ("conversations_month", "conversations/mo", True),
    ("whatsapp_numbers", "WhatsApp numbers", True),
    ("whatsapp_marketing_msgs", "WhatsApp marketing messages/mo", False),
    ("whatsapp_utility_msgs", "WhatsApp utility messages/mo", False),
    ("verification_codes_month", "verification codes/mo", False),
    ("emails_month", "emails/mo", True),
    ("emails_day", "emails/day", False),
    ("ai_actions_day", "AI actions/day", False),
    ("automation_rules", "active automations", False),
    ("contacts", "customers", False),
    ("team_members", "team members", False),
)


def plan_card(plan: Plan, *, channels=None, data=None) -> dict:
    """What a pricing card says about ``plan``: the channels it sells, its limits, the other
    features it includes in catalog order, and coming-soon teasers. The pricing page, signup
    wizard and landing page all use it, so what's advertised is what the plan gives.
    ``channels`` defaults to the ones offered to the public (on, and not restricted while Meta
    reviews the app).

    Every channel is listed the same way, on one line that comes first in ``limits``; channel
    features are left out of ``features`` so they aren't listed twice."""
    from apps.core.channels import (
        CHANNEL_FEATURE,
        feature_visible,
        limit_visible,
        public_channels,
    )

    if channels is None:
        channels = public_channels()

    # ``data``: plan_data() for several plans at once (the landing page), else this plan's own.
    data = (data or plan_data([plan]))[plan.pk]
    keys, values = data["keys"], data["values"]
    sellable = {f.key: f for f in catalog.matrix_features()}
    sold = [
        ch
        for ch, key in CHANNEL_FEATURE.items()
        if ch in channels and key in keys and key in sellable
    ]
    channel_names = [sellable[CHANNEL_FEATURE[ch]].name for ch in sold]
    limits = [f"Channels: {' · '.join(channel_names)}"] if channel_names else []
    for key, label, always in _PRICING_LIMITS:
        if not limit_visible(key, channels):
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
        and f.key not in catalog.CHANNEL_FEATURES
        and feature_visible(f.key, channels)
    ]
    return {
        "plan": plan,
        "channels": sold,
        "channel_names": channel_names,
        "limits": limits,
        "features": included,
        "feature_names": [f.name for f in included],
        "coming_soon": data["soon"],
    }


def _lower_first(name: str) -> str:
    """ "Team members" -> "team members", but "WhatsApp numbers" and "AI actions per day" keep
    their capitals (only a plain capitalised first word is lowered)."""
    first, _, rest = name.partition(" ")
    if first.istitle() and first not in ("Instagram", "Facebook", "Email"):
        first = first.lower()
    return f"{first} {rest}".strip()


# How many rows a signup plan card compares. Limits come first, then channels, then features.
_SIGNUP_ROWS = 6


def signup_comparison(offered, data=None) -> dict:
    """What the signup wizard's plan cards compare, for ``offered`` as
    ``accounts.forms.signup_offer`` returns it (plan, channels it gives on offer):

    - ``cards``: per plan slug, the same rows in the same order: only what differs between
      the plans on offer (limits, then channels, then features), each ``{"label", "ok"}``.
      A plan without a feature still gets its row, greyed out, so a free trial can never
      look like it gives more than a paid plan just by listing more lines.
    - ``included``: what every plan has, for one "All plans include" line under the cards.

    Message history isn't compared: Plan.log_retention_days isn't enforced (see _CARD_LIMITS).
    Like plan_card, it's read from the plans themselves."""
    from apps.core.channels import CHANNEL_FEATURE, feature_visible, limit_visible

    plans = [plan for plan, _ in offered]
    if data is None:
        data = plan_data(plans)
    gives = {plan.pk: set(channels) for plan, channels in offered}
    shown = frozenset().union(*gives.values()) if gives else frozenset()
    sellable = {f.key: f for f in catalog.matrix_features()}

    def limit_value(plan, key) -> int:
        return data[plan.pk]["values"].get(key, limit_catalog.get(key).default)

    limit_rows = [
        (key, label)
        for key, label, _ in _PRICING_LIMITS
        if limit_visible(key, shown) and len({limit_value(p, key) for p in plans}) > 1
    ]
    # (name, plans that have it) for every channel on offer and every other sellable feature.
    have: list[tuple[str, set]] = [
        (sellable[CHANNEL_FEATURE[ch]].name, {p.pk for p in plans if ch in gives[p.pk]})
        for ch in CHANNEL_FEATURE
        if ch in shown and CHANNEL_FEATURE[ch] in sellable
    ]
    have += [
        (f.name, {p.pk for p in plans if f.key in data[p.pk]["keys"]})
        for f in sellable.values()
        if f.key not in catalog.CHANNEL_FEATURES and feature_visible(f.key, shown)
    ]
    everyone = {p.pk for p in plans}
    differing = [(name, pks) for name, pks in have if pks and pks != everyone]
    included = [name for name, pks in have if pks == everyone]

    room = max(0, _SIGNUP_ROWS - len(limit_rows[:3]))
    cards = {}
    for plan in plans:
        rows = [
            {
                "label": _limit_line(limit_value(plan, key), label),
                "ok": limit_value(plan, key) != 0,
            }
            for key, label in limit_rows[:3]
        ]
        rows += [
            {"label": name, "ok": plan.pk in pks} for name, pks in differing[:room]
        ]
        cards[plan.slug] = rows
    return {"cards": cards, "included": included if plans else []}


def plan_data(plans) -> dict:
    """Features, limits and coming-soon items for ``plans`` in three queries, keyed by plan id,
    for ``plan_card`` and ``pricing_table`` (one query per plan each, otherwise)."""
    from apps.billing.models import ComingSoonFeature, PlanLimit

    ids = [p.pk for p in plans]
    data: dict = {pk: {"keys": set(), "values": {}, "soon": []} for pk in ids}
    for pk, key in PlanFeature.objects.filter(plan_id__in=ids).values_list(
        "plan_id", "key"
    ):
        data[pk]["keys"].add(key)
    for pk, key, value in PlanLimit.objects.filter(plan_id__in=ids).values_list(
        "plan_id", "key", "value"
    ):
        data[pk]["values"][key] = value
    for item in (
        ComingSoonFeature.objects.filter(is_active=True, plans__in=ids)
        .annotate(plan_pk=models.F("plans"))
        .order_by("pk")
    ):
        data[item.plan_pk]["soon"].append(item)
    return data


# The limits a landing pricing card leads with, in order of preference. A card shows the first
# three whose values differ between the plans on offer, so it says what changes from plan to
# plan; every card shows the same three. Message history isn't one: Plan.log_retention_days
# isn't enforced anywhere, so the landing page doesn't advertise it as a difference.
_CARD_LIMITS = (
    "conversations_month",
    "whatsapp_numbers",
    "team_members",
    "contacts",
    "automation_rules",
    "ai_actions_day",
    "emails_month",
)


def pricing_table(offered, data=None) -> dict:
    """What the landing page's pricing section shows for ``offered``, a list of
    ``(plan, channels)`` pairs as ``accounts.forms.signup_offer`` returns them:

    - ``cards``: per plan, the same three limits (those that differ between plans first), its
      tagline, the annual price checkout charges (``pricing.price_for``, a whole amount) and
      its monthly equivalent for display (``pricing.monthly_equivalent``);
    - ``included``: what every plan has: features and channels on every plan, and limits that
      are unlimited on all of them (the "All plans include" line);
    - ``rows``: the comparison table, only what differs between plans: a row per limit, channel,
      feature and coming-soon item, with a cell per plan (a number or text, True / False for a
      tick, or "soon");
    - ``annual_save_pct``: the annual saving, from the same multiplier checkout uses.

    ``data`` is ``plan_data(plans)`` when the caller already has it. Like ``plan_card``, it's
    read from the plans themselves, so nothing is promised that a plan doesn't give."""
    from apps.billing.models import Subscription
    from apps.billing.pricing import (
        PERIOD_BILLED,
        monthly_equivalent,
        multiplier_for,
        price_for,
    )
    from apps.core.channels import CHANNEL_FEATURE, feature_visible, limit_visible

    plans = [plan for plan, _ in offered]
    if data is None:
        data = plan_data(plans)
    shown = frozenset().union(*(set(channels) for _, channels in offered))

    def limit_value(plan, key) -> int:
        return data[plan.pk]["values"].get(key, limit_catalog.get(key).default)

    def limit_text(value: int) -> str:
        return "Unlimited" if value == limit_catalog.UNLIMITED else f"{value:,}"

    def differs(cells) -> bool:
        return len(set(cells)) > 1

    labels = {key: label for key, label, _ in _PRICING_LIMITS}
    candidates = [k for k in _CARD_LIMITS if limit_visible(k, shown)]
    differing = [k for k in candidates if differs([limit_value(p, k) for p in plans])]
    card_keys = (differing + [k for k in candidates if k not in differing])[:3]

    sellable = catalog.matrix_features()
    channel_keys = {CHANNEL_FEATURE[ch] for ch in CHANNEL_FEATURE if ch in shown}
    feature_names = {
        f.key: f.name
        for f in sellable
        if f.key in channel_keys
        or (f.key not in catalog.CHANNEL_FEATURES and feature_visible(f.key, shown))
    }

    def points(plan, prev) -> list[str]:
        """What a card lists under its headline limits (``key_limits``, the same three rows on
        every card, so cards line up and none looks unfinished): for the first paid plan, its
        channels; for each one after, only what it adds to the plan before ("Everything in
        Starter, plus:"), leaving out the headline limits it already shows."""
        mine = data[plan.pk]["keys"]
        if prev is None:
            lines = []
            channels = [
                n for k, n in feature_names.items() if k in channel_keys and k in mine
            ]
            # Channels every plan has go in "All plans include" instead.
            on_all = all(
                k in data[p.pk]["keys"] for p in paid for k in channel_keys if k in mine
            )
            if channels and not on_all:
                lines.append("Channels: " + ", ".join(channels))
            return lines
        theirs = data[prev.pk]["keys"]
        lines = [
            _limit_line(limit_value(plan, k), labels[k])
            for k in candidates
            if k not in card_keys and limit_value(plan, k) != limit_value(prev, k)
        ]
        lines += [
            f"Adds {n}" if k in channel_keys else n
            for k, n in feature_names.items()
            if k in mine and k not in theirs
        ]
        return lines[:5]

    multiplier = multiplier_for(Subscription.ANNUALLY)
    paid = [plan for plan in plans if plan.price_monthly > 0]
    cards = []
    for plan, _channels in offered:
        annual = price_for(plan, Subscription.ANNUALLY)
        prev = paid[paid.index(plan) - 1] if plan in paid and paid.index(plan) else None
        cards.append(
            {
                "plan": plan,
                "tagline": plan.tagline,
                "key_limits": [
                    _limit_line(limit_value(plan, k), labels[k]) for k in card_keys
                ],
                "points": points(plan, prev),
                "plus_from": prev,
                "annual_total": annual,
                "annual_billed": PERIOD_BILLED[Subscription.ANNUALLY],
                "annual_monthly": monthly_equivalent(plan, Subscription.ANNUALLY),
            }
        )

    rows: list[dict] = []
    included: list[str] = []
    unlimited: list[str] = []

    def add(group: str, label: str, cells: list, same: str) -> None:
        if differs(cells):
            rows.append({"group": group, "label": label, "cells": cells})
        elif same and same not in included:
            included.append(same)

    for f in sellable:
        if f.key in channel_keys:
            cells = [f.key in data[p.pk]["keys"] for p in plans]
            add("Channels", f.name, cells, f.name if all(cells) else "")
    for key, _, _ in _PRICING_LIMITS:
        if not limit_visible(key, shown):
            continue
        spec = limit_catalog.get(key)
        values = [limit_value(p, key) for p in plans]
        # "AI actions per day" already says its period; "Customer conversations" doesn't.
        label = spec.name
        if spec.period == limit_catalog.MONTH and "month" not in label.lower():
            label += " per month"
        elif spec.period == limit_catalog.DAY and "day" not in label.lower():
            label += " per day"
        if values and not differs(values) and values[0] == limit_catalog.UNLIMITED:
            # One bold line above the grid: "Unlimited team members and customers".
            unlimited.append(_lower_first(spec.name))
            continue
        add("Limits", label, [limit_text(v) for v in values], "")
    for f in sellable:
        if f.key in catalog.CHANNEL_FEATURES or not feature_visible(f.key, shown):
            continue
        cells = [f.key in data[p.pk]["keys"] for p in plans]
        if any(cells):
            add("Features", f.name, cells, f.name)
    soon: dict[str, set[int]] = {}
    for plan in plans:
        for item in data[plan.pk]["soon"]:
            soon.setdefault(item.name, set()).add(plan.pk)
    for name, plan_ids in soon.items():
        rows.append(
            {
                "group": "Coming soon",
                "label": name,
                "cells": ["soon" if p.pk in plan_ids else False for p in plans],
            }
        )
    return {
        "cards": cards,
        "paid_count": len(paid),
        "included": included,
        # Team members and customers first: what a buyer asks about.
        "unlimited": sorted(
            unlimited,
            key=lambda n: {"team members": 0, "customers stored": 1}.get(n, 2),
        ),
        "rows": rows,
        "annual_save_pct": round((1 - float(multiplier)) * 100),
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


def compare_plans(plans, *, channels=None) -> list[dict]:
    """Rows for a features x plans comparison table: [{feature, cells: [bool per plan]}]."""
    from apps.core.channels import feature_visible, public_channels

    if channels is None:
        channels = public_channels()

    plans = list(plans)
    matrix = plan_feature_matrix()
    rows = []
    for f in catalog.matrix_features():
        if not feature_visible(f.key, channels):
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


# ── Home ─────────────────────────────────────────────────────────────────────

HOME_NOTICE_DAYS = 7


def home_notice(subscription, now=None) -> dict | None:
    """What the plan needs from the owner, for a slim line on Home, or None.

    Home shows billing only when something needs action: no plan, a payment that didn't go
    through, a renewal that's due but hasn't gone through yet (never "Active · renews" on a
    date already past), a trial or a cancelled plan ending soon, or a renewal within
    HOME_NOTICE_DAYS. ``{"tone": "warning"|"info", "text", "action", "href"}``.
    """
    from django.utils import timezone

    from apps.billing.models import Subscription

    now = now or timezone.now()
    plans = "/billing/plans/"
    if subscription is None:
        return {
            "tone": "info",
            "text": "No active plan.",
            "action": "Choose a plan",
            "href": plans,
        }
    end = subscription.current_period_end

    def day(when) -> str:
        if not when:
            return ""
        local = timezone.localtime(when)
        return f"{local.day} {local:%b}"

    if subscription.status == Subscription.PAST_DUE:
        keep = _pay_by(subscription)
        return {
            "tone": "warning",
            "text": f"Your {subscription.plan.name} renewal is unpaid."
            + (f" Pay by {day(keep)} to keep your features." if keep else ""),
            "action": "Pay now",
            "href": plans,
        }
    if subscription.status == Subscription.TRIALING:
        trial_end = subscription.trial_ends_at or end
        if trial_end and trial_end - now <= timedelta(days=HOME_NOTICE_DAYS):
            return {
                "tone": "info" if trial_end > now else "warning",
                "text": f"Your trial ends {day(trial_end)}."
                if trial_end > now
                else f"Your trial ended {day(trial_end)}.",
                "action": "Choose a plan",
                "href": plans,
            }
        return None
    if subscription.status != Subscription.ACTIVE or not end:
        return None
    if subscription.cancel_at_period_end:
        if end - now <= timedelta(days=HOME_NOTICE_DAYS):
            return {
                "tone": "warning",
                "text": f"Your {subscription.plan.name} plan ends {day(end)}.",
                "action": "Keep it",
                "href": plans,
            }
        return None
    if end <= now and _awaiting_provider_renewal(subscription, now):
        return None  # the provider charges on its own schedule; nothing for the owner to do
    if end <= now:
        # Renewing: a provider-run renewal waits up to two days, a charge may be settling,
        # and the hourly renewal job moves it on. Say so, never "Active · renews <past>".
        return {
            "tone": "warning",
            "text": f"Your {subscription.plan.name} renewal was due {day(end)} and hasn't gone through yet.",
            "action": "Check billing",
            "href": plans,
        }
    if end - now <= timedelta(days=HOME_NOTICE_DAYS):
        return {
            "tone": "info",
            "text": f"Your {subscription.plan.name} plan renews {day(end)}.",
            "action": "Manage plan",
            "href": plans,
        }
    return None


def _pay_by(subscription):
    """The last moment an unpaid renewal keeps its features (past_due_since + grace)."""
    if not subscription.past_due_since:
        return None
    from apps.billing.renewals import grace_period

    return subscription.past_due_since + grace_period()


def _awaiting_provider_renewal(subscription, now) -> bool:
    """A provider-run recurring subscription renews this plan and its charge is still within
    the normal wait after the period end (renewals.RECURRING_GRACE)."""
    if not subscription.recurring_id or not subscription.current_period_end:
        return False
    from apps.billing.renewals import RECURRING_GRACE

    return now < subscription.current_period_end + RECURRING_GRACE


def plan_status(subscription, now=None) -> dict | None:
    """The plan's state as Settings › Plan & usage shows it: ``{"label", "tone", "line"}``,
    tone one of success/info/warning/danger/neutral. The same rules as ``home_notice``, so the
    two never disagree: an active plan whose period has ended reads "Renewal pending", never
    "Active · Renews <a date already past>"."""
    from django.utils import timezone

    from apps.billing.models import Subscription

    if subscription is None:
        return None
    now = now or timezone.now()
    end = subscription.current_period_end
    name = subscription.plan.name

    def day(when) -> str:
        local = timezone.localtime(when)
        return f"{local.day} {local:%b} {local.year}"

    status = subscription.status
    if status == Subscription.PAST_DUE:
        keep = _pay_by(subscription)
        if keep and keep <= now:
            return {
                "label": "Ended",
                "tone": "danger",
                "line": "Your renewal wasn't paid. Pay to switch your features back on.",
            }
        return {
            "label": "Payment due",
            "tone": "danger",
            "line": "Your renewal is unpaid. "
            + (
                f"Pay by {day(keep)} to keep your features."
                if keep
                else "Pay to keep your features."
            ),
        }
    if status == Subscription.INCOMPLETE:
        return {
            "label": "Payment needed",
            "tone": "warning",
            "line": f"Pay to switch on {name}.",
        }
    if status == Subscription.TRIALING:
        trial_end = subscription.trial_ends_at or end
        if trial_end and trial_end <= now:
            return {
                "label": "Trial ended",
                "tone": "warning",
                "line": f"Your trial ended {day(trial_end)}.",
            }
        return {
            "label": "Trial",
            "tone": "info",
            "line": f"Trial ends {day(trial_end)}." if trial_end else "Free trial.",
        }
    if status == Subscription.ACTIVE:
        if not end:
            return {"label": "Active", "tone": "success", "line": ""}
        if subscription.cancel_at_period_end:
            return {
                "label": "Ending",
                "tone": "warning",
                "line": f"Ends {day(end)} · won't renew.",
            }
        if end <= now and _awaiting_provider_renewal(subscription, now):
            return {
                "label": "Renewing",
                "tone": "info",
                "line": "Renewing automatically. The payment provider takes it today or "
                "tomorrow; nothing to do.",
            }
        if end <= now:
            return {
                "label": "Renewal pending",
                "tone": "warning",
                "line": f"Renewal was due {day(end)} and hasn't gone through yet.",
            }
        return {"label": "Active", "tone": "success", "line": f"Renews {day(end)}."}
    return {
        "label": subscription.get_status_display(),
        "tone": "danger" if status == Subscription.EXPIRED else "neutral",
        "line": "",
    }
