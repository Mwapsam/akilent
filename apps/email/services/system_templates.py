"""Rendering entry point for system/transactional emails.

Looks up an active SystemEmailTemplate by key and renders it through the
same sandboxed engine used for tenant EmailTemplates. Falls back to the
original file-based render_to_string(...) template if no active row exists,
so callers get identical behavior whether or not the DB row has been seeded
yet (or was deliberately deactivated/deleted).
"""

from __future__ import annotations

import functools
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
        if row.html_body:
            html_body = render_string(row.html_body, render_ctx)
        elif fallback_html_template and _is_seeded_text(row):
            # The seeded rows carry text only. While nobody has edited that text, the
            # designed file template says the same thing, so it is the HTML part.
            html_body = render_to_string(fallback_html_template, render_ctx)
        else:
            # An operator rewrote the text: their words must be what HTML readers see too,
            # so wrap that text in the branded shell rather than the file's old copy.
            from apps.email.services.html_email import build_transactional_html

            html_body = build_transactional_html(text_body, subject)
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


def _is_seeded_text(row) -> bool:
    """Whether the row's subject and text are still exactly what migration 0015 seeded."""
    seeded = _seeded_rows().get(row.key)
    return seeded is not None and (
        row.text_body == seeded["text_body"] and row.subject == seeded["subject"]
    )


@functools.cache
def _seeded_rows() -> dict[str, dict]:
    import importlib

    module = importlib.import_module("apps.email.migrations.0015_systememailtemplate")
    return {r["key"]: r for r in module.SEED_ROWS}
