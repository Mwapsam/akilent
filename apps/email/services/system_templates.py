"""Rendering entry point for system/transactional emails.

Looks up an active SystemEmailTemplate by key and renders it through the
same sandboxed engine used for tenant EmailTemplates. Falls back to the
original file-based render_to_string(...) template if no active row exists,
so callers get identical behavior whether or not the DB row has been seeded
yet (or was deliberately deactivated/deleted).
"""

from __future__ import annotations

import logging

from django.template.loader import render_to_string

from apps.email.services.render import render_string

logger = logging.getLogger(__name__)


def render_system_email(
    key: str,
    variables: dict | None = None,
    *,
    fallback_subject_template: str,
    fallback_body_template: str,
    fallback_html_template: str = "",
) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) for the system email identified by ``key``.

    Tries an active SystemEmailTemplate DB row first; if none exists, renders
    the fallback file templates. Branding context (site_name, logo_url, etc.)
    is merged into the template variables automatically so HTML templates that
    extend email/base_email.html have everything they need.

    ``html_body`` is ``""`` when no HTML template is available.
    """
    from apps.email.models import SystemEmailTemplate
    from apps.email.services.html_email import site_email_context

    variables = variables or {}
    branding = site_email_context()
    # Merge branding last so caller-supplied variables take precedence.
    render_ctx = {**branding, **variables}

    row = SystemEmailTemplate.objects.filter(key=key, is_active=True).first()
    if row is not None:
        subject = render_string(row.subject, render_ctx).strip()
        text_body = render_string(row.text_body, render_ctx)
        html_body = render_string(row.html_body, render_ctx) if row.html_body else ""
        return subject, text_body, html_body

    logger.warning(
        "render_system_email: no active SystemEmailTemplate for key=%s, "
        "falling back to file template",
        key,
    )
    subject = render_to_string(fallback_subject_template, render_ctx).strip()
    text_body = render_to_string(fallback_body_template, render_ctx)
    html_body = (
        render_to_string(fallback_html_template, render_ctx)
        if fallback_html_template
        else ""
    )
    return subject, text_body, html_body
