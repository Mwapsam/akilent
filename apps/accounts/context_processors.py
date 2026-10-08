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


_B2B_SECTION_ORDER = ["leads", "conversations", "customers", "campaigns", "sales"]
_B2C_SECTION_ORDER = ["customers", "campaigns", "conversations", "leads", "sales"]
_DEFAULT_SECTION_ORDER = ["conversations", "leads", "customers", "campaigns", "sales"]


def workspace_context(request):
    """Business-model-aware personalisation signals for templates.

    - ``dashboard_section_order``: list of section keys ordered for this business type.
      Templates that render multiple named sections can use this to place the most
      relevant section first (B2C: customers → campaigns; B2B: leads → conversations).
    - ``workspace_objectives``: the account's selected objective keys from BusinessContext,
      for surfacing capability suggestions inside the UI.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    try:
        from apps.accounts.utils import get_current_account

        account = get_current_account(request)
        if account is None:
            return {}
        ctx = getattr(account, "business_context", None)
        if ctx is None:
            return {
                "dashboard_section_order": list(_DEFAULT_SECTION_ORDER),
                "workspace_objectives": [],
            }
        if ctx.business_model == "b2b":
            order = _B2B_SECTION_ORDER
        elif ctx.business_model in ("b2c", "both"):
            order = _B2C_SECTION_ORDER
        else:
            order = _DEFAULT_SECTION_ORDER
        return {
            "dashboard_section_order": list(order),
            "workspace_objectives": list(ctx.objectives or []),
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
