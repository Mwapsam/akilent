"""Numbered template versions + rollback."""
from __future__ import annotations

from django.db import transaction
from django.db.models import Max


def snapshot_version(template, *, created_by=None, label: str = "", make_active: bool = True):
    """Record the template's current content as the next numbered version."""
    from apps.email.models import EmailTemplateVersion

    with transaction.atomic():
        last = (
            EmailTemplateVersion.objects.filter(template=template)
            .aggregate(n=Max("number"))["n"]
            or 0
        )
        if make_active:
            EmailTemplateVersion.objects.filter(template=template, is_active=True).update(
                is_active=False
            )
        return EmailTemplateVersion.objects.create(
            template=template,
            number=last + 1,
            label=label,
            is_active=make_active,
            subject=template.subject,
            text_body=template.text_body,
            html_body=template.html_body,
            content_blocks=template.content_blocks or {},
            created_by=created_by,
        )


def activate_version(template, number: int):
    """Roll the template's live content back to version ``number``."""
    from apps.email.models import EmailTemplateVersion

    version = EmailTemplateVersion.objects.get(template=template, number=number)
    with transaction.atomic():
        template.subject = version.subject
        template.text_body = version.text_body
        template.html_body = version.html_body
        template.content_blocks = version.content_blocks
        template.save(update_fields=["subject", "text_body", "html_body", "content_blocks"])
        EmailTemplateVersion.objects.filter(template=template, is_active=True).update(
            is_active=False
        )
        EmailTemplateVersion.objects.filter(pk=version.pk).update(is_active=True)
    version.refresh_from_db()
    return version
