"""Snapshot / rollback for BulkEmailCampaign content.

A snapshot is taken automatically when a campaign is submitted (see
apps.email.services.bulk.create_campaign). Drafts can be restored to any
snapshot; a campaign that has already left DRAFT is immutable.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from apps.email.models import BulkEmailCampaign, BulkEmailCampaignVersion

_CONTENT_FIELDS = (
    "from_email", "subject_override", "text_override", "html_override",
)


def _active_template_version_number(template) -> int | None:
    if template is None:
        return None
    row = template.versions.filter(is_active=True).values_list("number", flat=True).first()
    return row


@transaction.atomic
def snapshot_campaign(campaign: BulkEmailCampaign, *, created_by=None, label: str = "") -> BulkEmailCampaignVersion:
    """Freeze the campaign's current content as the next numbered version."""
    next_number = (
        BulkEmailCampaignVersion.objects.filter(campaign=campaign)
        .aggregate(m=Max("number"))["m"] or 0
    ) + 1
    return BulkEmailCampaignVersion.objects.create(
        campaign=campaign,
        number=next_number,
        label=label,
        from_email=campaign.from_email,
        subject_override=campaign.subject_override,
        text_override=campaign.text_override,
        html_override=campaign.html_override,
        template=campaign.template,
        template_version_number=_active_template_version_number(campaign.template),
        recipient_count=campaign.recipient_count,
        status_at_snapshot=campaign.status,
        created_by=created_by,
    )


@transaction.atomic
def restore_campaign_version(campaign: BulkEmailCampaign, number: int) -> BulkEmailCampaign:
    """Copy a snapshot's content back onto the campaign. DRAFT campaigns only."""
    if campaign.status != BulkEmailCampaign.Status.DRAFT:
        raise ValueError("only draft campaigns can be rolled back")
    version = BulkEmailCampaignVersion.objects.get(campaign=campaign, number=number)
    for field in _CONTENT_FIELDS:
        setattr(campaign, field, getattr(version, field))
    campaign.template = version.template
    campaign.save(update_fields=[*_CONTENT_FIELDS, "template"])
    return campaign
