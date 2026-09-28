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
) -> tuple[str, str]:
    """Return (subject, body) for the system email identified by `key`.

    Tries an active apps.email.models.SystemEmailTemplate row first; if none
    exists, renders `fallback_subject_template`/`fallback_body_template` via
    the standard Django template loader exactly as today's call sites do.
    """
    from apps.email.models import SystemEmailTemplate

    variables = variables or {}
    row = SystemEmailTemplate.objects.filter(key=key, is_active=True).first()
    if row is not None:
        subject = render_string(row.subject, variables).strip()
        body = render_string(row.text_body, variables)
        return subject, body

    logger.warning(
        "render_system_email: no active SystemEmailTemplate for key=%s, "
        "falling back to file template",
        key,
    )
    subject = render_to_string(fallback_subject_template, variables).strip()
    body = render_to_string(fallback_body_template, variables)
    return subject, body
