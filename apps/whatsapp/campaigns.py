"""WhatsApp bulk sends — the WhatsApp half of the channel-neutral "Campaigns"
concept (R1.5c). See ``apps.whatsapp.models.campaign.WhatsAppCampaign`` for
why this is intentionally MVP-sized rather than mirroring every
``apps.email`` bulk-campaign capability.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction

from apps.whatsapp.models import MessageTemplate, WhatsAppCampaign, WhatsAppContact

logger = logging.getLogger(__name__)


class CampaignError(ValueError):
    """Raised for a campaign that can't be created as requested."""


def _resolve_campaign_variables(variables: list, mapping: dict, contact) -> dict:
    """Build a WhatsApp template's ``params`` for one recipient.

    Same convention as a Workflow ``send_whatsapp`` step's
    ``variable_mapping`` (``apps.automation.workflow_engine._resolve_variable_mapping``),
    minus the ``"context.<key>"`` form — a campaign has no workflow run to
    read a context from.
    """
    attrs = contact.attributes or {}
    params: dict = {}
    for var in variables or []:
        if var not in mapping:
            continue
        source = mapping[var]
        if isinstance(source, str) and source.startswith("contact."):
            params[var] = attrs.get(source[len("contact."):])
        else:
            params[var] = source
    return params


def create_and_queue_campaign(
    *, account, name: str, contact_list, template_id: int,
    variable_mapping: dict | None = None, created_by=None,
) -> WhatsAppCampaign:
    """Create a ``WhatsAppCampaign`` and enqueue its send.

    Raises ``CampaignError`` for anything the UI should show back to the user
    (no approved template, empty list) rather than a generic 500.
    """
    template = MessageTemplate.objects.filter(pk=template_id, account=account).first()
    if template is None:
        raise CampaignError("Choose a template.")
    if template.approval_status != MessageTemplate.ApprovalStatus.APPROVED:
        raise CampaignError(
            "This template isn't approved by Meta yet — only an approved template can be sent as a campaign."
        )

    recipient_count = contact_list.contacts.filter(account=account).count()
    if recipient_count == 0:
        raise CampaignError("This customer list is empty.")

    campaign = WhatsAppCampaign.objects.create(
        account=account, name=name.strip() or template.name, contact_list=contact_list,
        template=template, variable_mapping=variable_mapping or {},
        recipient_count=recipient_count, status=WhatsAppCampaign.Status.QUEUED,
        created_by=created_by,
    )
    transaction.on_commit(lambda: send_campaign.delay(campaign.id))
    return campaign


@shared_task
def send_campaign(campaign_id: int) -> None:
    """Fan a ``WhatsAppCampaign`` out to one ``OutboundMessage`` per recipient.

    "Sending" here means "queued via the existing outbound pipeline" — rate
    limiting, the 24h-window/consent policy, retries and delivery status all
    already live in ``apps.whatsapp.tasks.drain_outbound_queue`` and
    ``apps.automation.workflows.send_whatsapp_message``; this task only does
    the one-time fan-out and records the campaign-level counts.
    """
    from apps.automation.workflows import send_whatsapp_message

    try:
        campaign = WhatsAppCampaign.objects.select_related("template", "contact_list").get(pk=campaign_id)
    except WhatsAppCampaign.DoesNotExist:
        logger.warning("send_campaign: campaign %s no longer exists", campaign_id)
        return

    campaign.mark_sending()
    queued = skipped = 0
    contacts = campaign.contact_list.contacts.filter(account=campaign.account)
    for contact in contacts:
        wa_contact = WhatsAppContact.objects.filter(account=campaign.account, contact=contact).first()
        if wa_contact is None or wa_contact.opt_in_status == WhatsAppContact.OptInStatus.OPTED_OUT:
            # No WhatsApp identity to send to, or they've opted out — never overridden here.
            skipped += 1
            continue
        params = _resolve_campaign_variables(
            campaign.template.variables, campaign.variable_mapping, contact
        )
        try:
            send_whatsapp_message(
                campaign.account, phone=wa_contact.phone_number,
                template_id=campaign.template_id, params=params,
            )
            queued += 1
        except Exception:
            logger.exception(
                "send_campaign: failed to queue campaign=%s contact=%s", campaign.pk, contact.pk
            )
            skipped += 1

    campaign.mark_completed(queued=queued, skipped=skipped)
