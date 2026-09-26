from django.conf import settings


def feature_flags(request):
    """Expose the soft-disable feature flags to all templates."""
    return {
        "WHATSAPP_ENABLED": settings.WHATSAPP_ENABLED,
    }


def plan_features(request):
    """What the nav needs from the business's entitlements:

    - ``locked_features``: catalog keys the business isn't entitled to. Their links stay visible
      with a lock and open the locked page, which says where to get them.
    - ``tools_off``: optional tools the owner switched off in Settings; their links are hidden.
    - ``usable_features``: entitled and not switched off, for in-page offers (e.g. "Track as a
      sale" only when Sales is usable).

    Defensive like ``onboarding_status``: never breaks rendering.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    try:
        from apps.accounts.utils import get_current_account
        from apps.billing import api as billing_api

        account = get_current_account(request)
        if account is None:
            return {}
        state = billing_api.nav_state(account)
        return {
            "locked_features": state["locked"],
            "tools_off": state["tools_off"],
            "usable_features": state["usable"],
        }
    except Exception:
        return {}


def onboarding_status(request):
    """Expose onboarding progress for the floating widget + welcome tour.

    Only returns ``onboarding`` while there's still required setup to do, so the
    widget/tour disappear once the workspace is set up. Defensive: never breaks
    rendering (anonymous users, no workspace, un-migrated DB).
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    try:
        from apps.accounts import onboarding as ob
        from apps.accounts.utils import get_current_account

        account = get_current_account(request)
        if account is None:
            return {}
        state = ob.get_state(account)
        extra = {
            "onboarding_wants_whatsapp": ob._wants_whatsapp(account),
            "onboarding_wants_email": ob._wants_email(account),
        }
        if state["complete"]:
            return {}
        return dict({"onboarding": state}, **extra)
    except Exception:
        return {}
