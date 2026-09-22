"""Public API for the billing module.

This is the single interface other modules should use to check feature availability,
get subscription info, and query usage.

Direct imports of apps.billing.models are not allowed outside of billing.
"""
import logging
from typing import Optional

from apps.accounts.models import Account
from apps.billing.models import ModuleSubscription, Plan, Subscription, UsageSummary

logger = logging.getLogger(__name__)

# Map public feature names to ModuleSubscription module choices
MODULE_MAP = {
    "whatsapp": ModuleSubscription.WHATSAPP,
    "email": ModuleSubscription.EMAIL,
    "automation": ModuleSubscription.AUTOMATION,
    "payments": ModuleSubscription.PAYMENTS,
    "ai": ModuleSubscription.AI,
    "crm": ModuleSubscription.CRM,
    "commerce": ModuleSubscription.COMMERCE,
}

# Legacy Plan attribute fallback map (for backward compatibility during migration)
# Only features that still have a Plan column belong here. "whatsapp" and "automation"
# have no Plan boolean (the columns never existed): they are answered by
# ModuleSubscription alone, which the 0013 seed populated for existing accounts.
LEGACY_PLAN_MAP = {
    "email": "email_apis",
    "inbound_email": "inbound_email",
    "bulk_email": "bulk_email",
    "email_templates": "email_templates",
    "detailed_analytics": "detailed_analytics",
    "api_access": "email_apis",
    "outbound_webhooks": "outbound_webhooks",
}


def has_feature(account: Account, feature_name: str) -> bool:
    """Check if an account has a feature enabled.

    Checks ModuleSubscription first (Phase 4+), falls back to legacy Plan
    booleans for backward compatibility during the migration period.

    Call hierarchy:
      1. Check ModuleSubscription(account, module).enabled
      2. Fall back to Plan.{feature_bool} if not found (logs fallback-hit)
      3. Return False if neither exists

    Args:
        account: The account to check
        feature_name: Feature name (e.g. 'whatsapp', 'email', 'automation')

    Returns:
        True if the feature is enabled for this account
    """
    # Step 1: Check ModuleSubscription first
    module = MODULE_MAP.get(feature_name)
    if module:
        try:
            mod_sub = ModuleSubscription.objects.get(account=account, module=module)
            return mod_sub.enabled
        except ModuleSubscription.DoesNotExist:
            pass  # Fall through to legacy check

    # Step 2: Fall back to legacy Plan boolean (with fallback-hit logging)
    try:
        subscription = Subscription.objects.get(account=account)
        plan = subscription.plan
    except Subscription.DoesNotExist:
        return False

    # Map feature to plan attribute
    attr = LEGACY_PLAN_MAP.get(feature_name)
    if attr is None:
        return False

    result = getattr(plan, attr, False)
    if result:
        # Log that we hit the fallback path so we know when migration is complete
        logger.debug(
            "has_feature fallback hit: account=%s feature=%s using Plan.%s",
            account.slug, feature_name, attr
        )

    return result


def module_enabled(account: Account, feature_name: str) -> bool:
    """Whether ``account`` may use a module (views, workflow enrollment, actions).

    Backward compatible: only an explicit ``ModuleSubscription(enabled=False)`` row
    blocks a module. A tenant with no row is allowed, so existing tenants keep
    working (crm/commerce were never seeded). Unlike ``has_feature`` this is not a
    plan-entitlement check.

    Raises:
        KeyError: if ``feature_name`` isn't a recognized module.
    """
    module = MODULE_MAP[feature_name]
    row = ModuleSubscription.objects.filter(account=account, module=module).first()
    return row is None or row.enabled


def enable_module(account: Account, feature_name: str) -> None:
    """Enable a module (``ModuleSubscription.enabled = True``) for an account.

    Idempotent. Used by ``apps.verticals`` when activating a Starter pack —
    the public entry point so callers never import ``ModuleSubscription``
    directly.

    Raises:
        KeyError: if ``feature_name`` isn't a recognized module.
    """
    module = MODULE_MAP[feature_name]
    ModuleSubscription.objects.update_or_create(account=account, module=module, defaults={"enabled": True})


def set_module_enabled(account: Account, feature_name: str, enabled: bool) -> None:
    """Set a module on or off for an account — the self-serve counterpart to
    ``enable_module`` (which only ever turns one on). Used by the account's own
    "Optional tools" settings page (apps.accounts.settings_views.settings_tools),
    as distinct from the platform-admin toggle in apps.core.views.

    Idempotent, and safe to call for a module that already has no row (leaves it
    enabled, per ``module_enabled``'s "no row = enabled" rule) when ``enabled=True``.

    Raises:
        KeyError: if ``feature_name`` isn't a recognized module.
    """
    module = MODULE_MAP[feature_name]
    ModuleSubscription.objects.update_or_create(account=account, module=module, defaults={"enabled": enabled})


def get_subscription(account: Account) -> Optional[Subscription]:
    """Get the subscription for an account.

    Returns:
        Subscription instance or None if no subscription exists
    """
    return Subscription.objects.filter(account=account).first()


def get_plan(account: Account) -> Optional[Plan]:
    """Get the plan for an account.

    Returns:
        Plan instance or None if no subscription/plan exists
    """
    subscription = Subscription.objects.filter(account=account).first()
    return subscription.plan if subscription else None


def get_current_usage(account: Account) -> Optional[UsageSummary]:
    """Get current usage for an account."""
    return UsageSummary.objects.filter(account=account).first()


def get_email_usage(account: Account) -> int:
    """Get total email usage for an account (emails sent this billing period)."""
    return UsageSummary.get_current_email_usage(account)
