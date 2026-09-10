"""Bulk campaign creation.

Mirrors apps.email.webhooks.enqueue_event's "create rows, then dispatch"
shape, adapted for potentially large recipient lists: rows are bulk_created
in batches and the actual per-recipient send fan-out is chunked inside the
dispatch_campaign Celery task rather than done inline here, so this stays
fast regardless of recipient count.
"""
from __future__ import annotations

from django.db import transaction

from apps.email.exceptions import UnverifiedDomainError
from apps.email.models import BulkEmailCampaign, BulkEmailRecipient, EmailDomain

_CREATE_BATCH_SIZE = 1000


def create_campaign(
    *,
    account,
    from_email: str,
    template=None,
    subject_override: str = "",
    text_override: str = "",
    html_override: str = "",
    recipients: list[dict],
    initial_status: str | None = None,
) -> BulkEmailCampaign:
    """Validate the sending domain, create the campaign + recipient rows.

    `recipients` is a list of {"to": str, "variables": dict}. Does not touch
    quota — reservation happens per-chunk in dispatch_campaign, not here, so
    creating a campaign can't be used to probe/burn quota without actually
    queueing sends.
    """
    from apps.email.tasks import dispatch_campaign

    from_domain = from_email.rsplit("@", 1)[-1].lower()
    domain = EmailDomain.objects.filter(
        account=account, domain=from_domain, status=EmailDomain.Status.VERIFIED
    ).first()
    if domain is None:
        raise UnverifiedDomainError(from_domain)

    scheduled = initial_status == BulkEmailCampaign.Status.SCHEDULED
    campaign = BulkEmailCampaign.objects.create(
        account=account,
        domain=domain,
        template=template,
        from_email=from_email,
        subject_override=subject_override,
        text_override=text_override,
        html_override=html_override,
        recipient_count=len(recipients),
        status=initial_status or BulkEmailCampaign.Status.DRAFT,
    )

    rows = [
        BulkEmailRecipient(
            campaign=campaign,
            to_email=r["to"],
            variables=r.get("variables") or {},
        )
        for r in recipients
    ]
    BulkEmailRecipient.objects.bulk_create(rows, batch_size=_CREATE_BATCH_SIZE)

    from apps.email.services.campaign_versions import snapshot_campaign

    snapshot_campaign(campaign, label="scheduled" if scheduled else "submitted")

    # A scheduled campaign is dispatched later by apps.scheduler.drainer, which
    # flips it SCHEDULED -> QUEUED and calls dispatch_campaign then.
    if not scheduled:
        transaction.on_commit(lambda: dispatch_campaign.delay(campaign.id))
    return campaign
