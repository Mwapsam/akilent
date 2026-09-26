from django.conf import settings


def site_context(request):
    """Expose branding + feature flags to every template.

    WhatsApp follows the WHATSAPP_ENABLED environment setting only: URLs and Celery routes are
    wired from it at boot, so a database switch could only hide parts of the screens, never really
    turn WhatsApp off. Defensive: never breaks rendering if the table isn't migrated yet.
    """
    site = None
    signups = True
    try:
        from apps.core.models import SiteSettings

        site = SiteSettings.load()
        signups = site.signups_enabled
    except Exception:
        pass

    return {
        "site": site,
        "WHATSAPP_ENABLED": settings.WHATSAPP_ENABLED,
        "SIGNUPS_ENABLED": signups,
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
