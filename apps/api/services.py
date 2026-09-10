"""Shared business logic for sending a transactional message via the API.

Used by both the versioned apps.api.views.MessageCreateView and the legacy
apps.email.views.api_send shim, so the two entry points can never drift.
"""
from __future__ import annotations

from django.db import transaction
from django.utils.text import slugify

from apps.billing.limits import LimitChecker
from apps.email.exceptions import UnverifiedDomainError
from apps.email.models import BulkEmailCampaign, EmailDomain, EmailMessage, EmailTemplate
from apps.email.services import render_template, validate_variables
from apps.email.services.bulk import create_campaign
from apps.email.tasks import send_email

# Re-exported for backward compat — this used to be defined here.
__all__ = [
    "UnverifiedDomainError",
    "create_and_queue_message",
    "create_template",
    "update_template",
    "clone_template",
    "render_template_preview",
    "create_and_queue_campaign",
]


class TemplateMissingContentError(Exception):
    """Neither template_id nor inline subject/text/html were provided."""


class RecipientCapExceededError(Exception):
    def __init__(self, cap: int, count: int):
        self.cap = cap
        self.count = count
        super().__init__(
            f"Campaign has {count} recipients, exceeding your plan's cap of {cap}."
        )


def create_and_queue_message(
    *,
    account,
    from_email: str,
    to_email: str,
    subject: str = "",
    text_body: str = "",
    html_body: str = "",
    template_id: int | None = None,
    template_variables: dict | None = None,
    mode: str = "live",
) -> EmailMessage:
    """Validate, reserve quota, and queue a transactional send.

    If template_id is given, the template's subject/text/html are rendered
    with template_variables and take precedence over subject/text_body/
    html_body. Raises UnverifiedDomainError or
    apps.billing.limits.PlanLimitExceeded on rejection — callers translate
    those into their own response shape.
    """
    is_test = mode == "test"

    from_domain = from_email.rsplit("@", 1)[-1].lower()
    domain = EmailDomain.objects.filter(
        account=account, domain=from_domain, status=EmailDomain.Status.VERIFIED
    ).first()
    # Test mode routes through the sandbox — no real delivery, so a verified
    # domain, plan quota, suppression, reputation and MX checks don't apply.
    if domain is None and not is_test:
        raise UnverifiedDomainError(from_domain)

    lc = LimitChecker(account)
    lc.require_feature("email_apis", "the email API & SMTP relay")
    if not is_test:
        lc.check_email()

        from apps.email.services.suppression import is_suppressed, record_event
        from apps.email.services.validation import validate_recipient

        if is_suppressed(account, to_email):
            raise UnverifiedDomainError(
                f"{to_email} is suppressed (bounce, complaint, or unsubscribe)"
            )

        from apps.email.services.reputation import check_can_send

        allowed, reason = check_can_send(account)
        if not allowed:
            raise UnverifiedDomainError(
                f"sending is paused for this account — sender reputation halt ({reason})"
            )

        if not validate_recipient(to_email):
            record_event(account=account, email=to_email, reason="invalid")
            raise UnverifiedDomainError(
                f"{to_email} failed validation (invalid syntax or no MX record)"
            )

    template = None
    if template_id is not None:
        template = EmailTemplate.objects.get(pk=template_id, account=account)
        subject, text_body, html_body = render_template(
            template, template_variables or {}
        )

    msg = EmailMessage.objects.create(
        account=account,
        domain=domain,
        template=template,
        from_email=from_email,
        to_email=to_email,
        subject=subject,
        key_mode="test" if is_test else "live",
        rendered_subject=subject,
        rendered_text=text_body,
        rendered_html=html_body,
    )

    from apps.logs.services import record_message_event

    record_message_event(msg, "queued", source="api")

    transaction.on_commit(
        lambda: send_email.delay(msg.id, text_body=text_body, html_body=html_body)
    )
    return msg


def create_template(
    *,
    account,
    name: str,
    slug: str = "",
    subject: str = "",
    text_body: str = "",
    html_body: str = "",
    sample_variables: dict | None = None,
    content_blocks: dict | None = None,
    builder_mode: str = "raw",
) -> EmailTemplate:
    lc = LimitChecker(account)
    lc.require_feature("email_templates", "email templates")
    return EmailTemplate.objects.create(
        account=account,
        name=name,
        slug=slug or slugify(name),
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        sample_variables=sample_variables or {},
        content_blocks=content_blocks or {},
        builder_mode=builder_mode,
    )


def update_template(*, template: EmailTemplate, **fields) -> EmailTemplate:
    field_map = {
        "name": "name",
        "subject": "subject",
        "text": "text_body",
        "html": "html_body",
        "sample_variables": "sample_variables",
        "content_blocks": "content_blocks",
        "builder_mode": "builder_mode",
    }
    updated = []
    for key, model_field in field_map.items():
        if key in fields and fields[key] is not None:
            setattr(template, model_field, fields[key])
            updated.append(model_field)
    if updated:
        template.save(update_fields=updated)
    return template


def clone_template(*, template: EmailTemplate) -> EmailTemplate:
    lc = LimitChecker(template.account)
    lc.require_feature("email_templates", "email templates")

    base_slug = slugify(f"{template.slug}-copy")
    slug = base_slug
    counter = 2
    while EmailTemplate.objects.filter(account=template.account, slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1

    return EmailTemplate.objects.create(
        account=template.account,
        name=f"{template.name} (copy)",
        slug=slug,
        subject=template.subject,
        text_body=template.text_body,
        html_body=template.html_body,
        content_blocks=template.content_blocks,
        builder_mode=template.builder_mode,
        sample_variables=template.sample_variables,
    )


def render_template_preview(*, template: EmailTemplate, variables: dict | None = None) -> dict:
    variables = variables or template.sample_variables
    subject, text_body, html_body = render_template(template, variables)
    return {
        "subject": subject,
        "text": text_body,
        "html": html_body,
        "missing_variables": validate_variables(template, variables),
    }


def create_and_queue_campaign(
    *,
    account,
    from_email: str,
    template_id: int | None = None,
    subject: str = "",
    text_body: str = "",
    html_body: str = "",
    recipients: list[dict] | None = None,
    list_slug: str | None = None,
    segment_slug: str | None = None,
) -> BulkEmailCampaign:
    """Validate, gate on plan features/recipient cap, and queue a bulk campaign.

    Either template_id or inline subject/text/html must be given. Raises
    UnverifiedDomainError, TemplateMissingContentError,
    RecipientCapExceededError, or apps.billing.limits.PlanLimitExceeded —
    callers translate those into their own response shape.
    """
    lc = LimitChecker(account)
    lc.require_feature("bulk_email", "bulk email sending")

    if recipients is None:
        if list_slug or segment_slug:
            from apps.contacts.audience import resolve_recipients

            try:
                recipients = resolve_recipients(
                    account, list_slug=list_slug, segment_slug=segment_slug
                )
            except ValueError as exc:
                raise TemplateMissingContentError(str(exc)) from exc
        else:
            recipients = []
    if not recipients:
        raise TemplateMissingContentError(
            "No recipients — provide `recipients`, `list`, or `segment`."
        )

    from apps.email.services.reputation import check_can_send

    allowed, reason = check_can_send(account)
    if not allowed:
        raise UnverifiedDomainError(
            f"sending is paused for this account — sender reputation halt ({reason})"
        )

    # require_feature already confirmed an active subscription exists.
    cap = lc.subscription.plan.max_bulk_recipients_per_campaign
    if cap != -1 and len(recipients) > cap:
        raise RecipientCapExceededError(cap, len(recipients))

    template = None
    if template_id is not None:
        template = EmailTemplate.objects.get(pk=template_id, account=account)
    elif not (subject or text_body or html_body):
        raise TemplateMissingContentError(
            "Provide either template_id or subject/text/html."
        )

    return create_campaign(
        account=account,
        from_email=from_email,
        template=template,
        subject_override=subject,
        text_override=text_body,
        html_override=html_body,
        recipients=recipients,
    )
