"""Helpers for building branded HTML bodies for system/transactional emails."""

from __future__ import annotations

import datetime


def site_email_context() -> dict:
    """Return branding context for the base email template.

    Safe to call from Celery tasks — reads SiteSettings once, builds an
    absolute logo URL from BASE_DOMAIN (no HTTP request needed).
    """
    from django.conf import settings

    from apps.core.models import SiteSettings

    row = SiteSettings.load()
    base_domain = getattr(settings, "BASE_DOMAIN", "") or (
        settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else "localhost"
    )
    scheme = "http" if settings.DEBUG else "https"
    base = f"{scheme}://{base_domain}"

    logo_url = None
    if row.logo:
        try:
            logo_url = f"{base}{row.logo.url}"
        except Exception:
            pass

    return {
        "site_name": row.app_name or "Automator",
        "logo_url": logo_url,
        "support_email": row.support_email or "",
        "year": datetime.datetime.now(tz=datetime.UTC).year,
        "base_url": base,
    }


def build_transactional_html(text_body: str, subject: str = "") -> str:
    """Wrap a plain-text email body in the branded Akilent shell.

    Splits ``text_body`` on blank lines into ``<p>`` blocks so the message
    renders readably. Used for all emails whose body is assembled as an
    f-string rather than a dedicated HTML template.
    """
    from django.template.loader import render_to_string

    paragraphs = [p.strip() for p in text_body.split("\n\n") if p.strip()]
    ctx = {
        **site_email_context(),
        "subject": subject,
        "paragraphs": paragraphs,
    }
    return render_to_string("email/generic_transactional.html", ctx)
