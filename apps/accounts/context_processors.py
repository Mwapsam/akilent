from django.conf import settings


def feature_flags(request):
    """Expose the soft-disable feature flags to all templates."""
    return {
        "WHATSAPP_ENABLED": settings.WHATSAPP_ENABLED,
    }


def module_flags(request):
    """Expose which optional modules (Sales/Orders) this account has turned on,
    so nav links can hide themselves rather than 404 or redirect with an error
    when a business has opted out (see apps.accounts.settings_views.settings_tools,
    R1.5a). Defensive like ``onboarding_status`` — never breaks rendering.
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
        return {
            "crm_enabled": billing_api.module_enabled(account, "crm"),
            "commerce_enabled": billing_api.module_enabled(account, "commerce"),
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
