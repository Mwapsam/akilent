from django.conf import settings


def site_context(request):
    """Expose branding + feature flags to every template."""
    site = None
    signups = True
    try:
        from apps.core.models import SiteSettings

        site = SiteSettings.load()
        signups = site.signups_enabled
    except Exception:
        pass

    from apps.core.channels import enabled_channels

    ec = enabled_channels()
    return {
        "site": site,
        "enabled_channels": ec,
        "WHATSAPP_ENABLED": "whatsapp" in ec,  # backward compat for existing templates
        # Campaigns and Templates are shared pages under /email/ that WhatsApp also uses.
        "CAMPAIGNS_ENABLED": bool(ec & {"email", "whatsapp"}),
        "SIGNUPS_ENABLED": signups,
        "REALTIME_SSE_ENABLED": settings.REALTIME_SSE_ENABLED,
    }


def operator_context(request):
    """``is_operator`` and ``viewing_as`` (the business an operator is viewing read-only)."""
    from apps.accounts.utils import viewing_as
    from apps.core.utils import is_operator

    operator = is_operator(getattr(request, "user", None))
    return {
        "is_operator": operator,
        "viewing_as": viewing_as(request) if operator else None,
    }
