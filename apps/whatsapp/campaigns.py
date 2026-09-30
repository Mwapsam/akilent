"""WhatsApp bulk sends — the WhatsApp half of the channel-neutral "Campaigns"
concept (R1.5c). See ``apps.whatsapp.models.campaign.WhatsAppCampaign`` and
``WhatsAppCampaignRecipient`` for the processing-vs-delivery split this module
implements.

Fan-out mirrors ``apps.email.services.bulk``/``apps.email.tasks.dispatch_campaign``:
``WhatsAppCampaignRecipient`` rows are created up front (``create_and_queue_campaign``)
and ``send_campaign`` processes them in bounded, restart-safe chunks rather than
looping over the whole list inside one task.
"""

from __future__ import annotations

import logging

from django.db import transaction

from apps.whatsapp.models import (
    MessageTemplate,
    OutboundMessage,
    WhatsAppCampaign,
    WhatsAppCampaignRecipient,
    WhatsAppContact,
)
from celery import shared_task

logger = logging.getLogger(__name__)

# Matches apps.email.tasks._CAMPAIGN_CHUNK_SIZE — same order-of-magnitude
# problem (bounded, restart-safe fan-out), no reason to pick a different
# number without a measured reason to.
_CAMPAIGN_CHUNK_SIZE = 500

_Status = WhatsAppCampaignRecipient.Status
_SkipReason = WhatsAppCampaignRecipient.SkipReason


class CampaignError(ValueError):
    """Raised for a campaign that can't be created as requested."""


def create_and_queue_campaign(
    *,
    account,
    name: str,
    contact_list,
    template_id: int,
    variable_mapping: dict | None = None,
    variable_fallbacks: dict | None = None,
    created_by=None,
) -> WhatsAppCampaign:
    """Create a ``WhatsAppCampaign`` + one ``WhatsAppCampaignRecipient`` per
    contact, and enqueue the first fan-out chunk.

    Raises ``CampaignError`` for anything the UI should show back to the user
    (no approved template, empty list) rather than a generic 500. Recipient
    rows are all ``PENDING`` — nothing is sent inline here, so this returns
    fast regardless of list size.
    """
    template = MessageTemplate.objects.filter(pk=template_id, account=account).first()
    if template is None:
        raise CampaignError("Choose a template.")
    if template.approval_status != MessageTemplate.ApprovalStatus.APPROVED:
        raise CampaignError(
            "This template isn't approved by Meta yet — only an approved template can be sent as a campaign."
        )

    contact_ids = list(
        contact_list.contacts.filter(account=account).values_list("id", flat=True)
    )
    if not contact_ids:
        raise CampaignError("This customer list is empty.")
    from apps.billing import api as billing_api

    if not billing_api.check_rule(
        account, "whatsapp_campaign_recipients", len(contact_ids)
    ):
        cap = billing_api.limit(account, "whatsapp_campaign_recipients")
        raise CampaignError(
            f"This list has {len(contact_ids):,} customers, but your plan sends a WhatsApp campaign to "
            f"at most {cap:,}. Choose a smaller list or upgrade your plan."
        )

    campaign = WhatsAppCampaign.objects.create(
        account=account,
        name=name.strip() or template.name,
        contact_list=contact_list,
        template=template,
        variable_mapping=variable_mapping or {},
        variable_fallbacks=variable_fallbacks or {},
        recipient_count=len(contact_ids),
        status=WhatsAppCampaign.Status.QUEUED,
        created_by=created_by,
    )
    WhatsAppCampaignRecipient.objects.bulk_create(
        [
            WhatsAppCampaignRecipient(campaign=campaign, contact_id=cid)
            for cid in contact_ids
        ],
        batch_size=1000,
    )
    transaction.on_commit(lambda: send_campaign.delay(campaign.id))
    return campaign


def _skip(
    recipient: WhatsAppCampaignRecipient, reason: str
) -> WhatsAppCampaignRecipient:
    recipient.status = _Status.SKIPPED
    recipient.skip_reason = reason
    return recipient


@shared_task(bind=True, max_retries=3, default_retry_delay=2, queue="campaigns")
def send_campaign(self, campaign_id: int) -> None:
    """Fan a ``WhatsAppCampaign`` out to ``OutboundMessage`` rows, one bounded
    chunk of ``PENDING`` recipients at a time, re-enqueuing itself until none
    remain.

    Restart-safe by construction: a chunk's claim, its OutboundMessage
    creation, its recipient-row updates, and the campaign's processing
    counters all happen inside one ``transaction.atomic()`` block, and the
    *next* chunk is only enqueued via ``transaction.on_commit`` — process,
    then commit, then enqueue. A worker dying mid-chunk leaves that chunk's
    rows exactly as they were (still PENDING); re-running this task just
    claims them again. Deliberately one active chunk chain per campaign, not
    parallelized — see the plan doc for why.

    "Sending" here means "queued via the existing outbound pipeline" — rate
    limiting, the 24h-window/consent policy, retries and delivery status all
    already live in ``apps.whatsapp.tasks.drain_outbound_queue`` and Meta's
    webhooks; this task only does the fan-out bookkeeping.
    """
    from apps.automation import variables
    from apps.automation.workflows import _build_template_payload

    campaign = WhatsAppCampaign.objects.select_related("template", "contact_list").get(
        pk=campaign_id
    )
    if campaign.status in (
        WhatsAppCampaign.Status.COMPLETED,
        WhatsAppCampaign.Status.FAILED,
    ):
        return
    campaign.mark_sending()

    with transaction.atomic():
        chunk = list(
            WhatsAppCampaignRecipient.objects.select_for_update()
            .filter(campaign=campaign, status=_Status.PENDING)
            .select_related("contact")
            .order_by("pk")[:_CAMPAIGN_CHUNK_SIZE]
        )

        if not chunk:
            done = not WhatsAppCampaignRecipient.objects.filter(
                campaign=campaign, status=_Status.PENDING
            ).exists()
        else:
            # Record progress before doing the work: if this chunk fails partway
            # through, the atomic block rolls this back too, so a stuck sweeper
            # re-check still sees the last *successful* chunk's timestamp.
            campaign.touch_progress()

            # One query for every WhatsAppContact this chunk needs, not one per recipient.
            wa_by_contact = {
                wc.contact_id: wc
                for wc in WhatsAppContact.objects.filter(
                    account=campaign.account,
                    contact_id__in=[r.contact_id for r in chunk],
                )
            }

            to_create: list[tuple] = []
            to_update: list[WhatsAppCampaignRecipient] = []
            for r in chunk:
                wc = wa_by_contact.get(r.contact_id)
                if wc is None:
                    to_update.append(_skip(r, _SkipReason.NO_WHATSAPP_IDENTITY))
                    continue
                if wc.opt_in_status == WhatsAppContact.OptInStatus.OPTED_OUT:
                    to_update.append(_skip(r, _SkipReason.OPTED_OUT))
                    continue
                try:
                    params = variables.resolve(
                        campaign.template.variables,
                        campaign.variable_mapping,
                        contact=r.contact,
                        account=campaign.account,
                        context={},
                        fallbacks=campaign.variable_fallbacks,
                    )
                except variables.MissingValue:
                    to_update.append(_skip(r, _SkipReason.MISSING_VALUE))
                    continue
                to_create.append((r, wc, params))

            # Build OutboundMessage rows via the shared payload helper (template
            # fetched once already via select_related) and bulk_create them,
            # instead of N calls into send_whatsapp_message (which re-fetches
            # the template every time).
            #
            # idempotency_key is deterministic (campaign+recipient), not a
            # random uuid: dedup here doesn't rely solely on select_for_update
            # only ever claiming PENDING rows — the DB's partial unique
            # constraint on (account, idempotency_key) is a second, independent
            # guard if a recipient is ever handed to this branch twice.
            outbound_rows = [
                OutboundMessage(
                    account=campaign.account,
                    contact=wc,
                    template=campaign.template,
                    payload=_build_template_payload(
                        campaign.template, params, sent_by="campaign"
                    ),
                    idempotency_key=f"campaign:{campaign.id}:recipient:{r.id}",
                )
                for r, wc, params in to_create
            ]
            created = OutboundMessage.objects.bulk_create(outbound_rows)
            for (r, _wc, _params), msg in zip(to_create, created):
                r.message = msg
                r.status = _Status.QUEUED
                to_update.append(r)

            WhatsAppCampaignRecipient.objects.bulk_update(
                to_update, ["status", "skip_reason", "error", "message"]
            )
            campaign.increment_counts(
                queued=len(to_create), skipped=len(to_update) - len(to_create)
            )
            done = not WhatsAppCampaignRecipient.objects.filter(
                campaign=campaign, status=_Status.PENDING
            ).exists()

        if done:
            campaign.mark_completed()

    if not done:
        transaction.on_commit(lambda: send_campaign.delay(campaign_id))


def campaign_delivery_stats(campaign: WhatsAppCampaign) -> dict:
    """sent / delivered / read / failed / pending counts for this campaign's
    QUEUED recipients, read live from OutboundMessage + MessageLog.

    Deliberately scoped to QUEUED recipients only — a SKIPPED/FAILED recipient
    was never handed to Meta and has no delivery outcome to report (see the
    skip breakdown instead). ``OutboundMessage.status`` is authoritative for
    terminal failure, not ``message_log.status``: every path that lands an
    OutboundMessage in FAILED also force-writes MessageLog.status to FAILED
    in the same branch (apps.whatsapp.tasks.drain_outbound_queue), but this
    does not assume that sync can never be missed.
    """
    from apps.whatsapp.models import MessageLog

    counts = {"sent": 0, "delivered": 0, "read": 0, "failed": 0, "pending": 0}
    recipients = WhatsAppCampaignRecipient.objects.filter(
        campaign=campaign, status=_Status.QUEUED
    ).select_related("message__message_log")
    for r in recipients:
        msg = r.message
        if msg is None:
            counts["pending"] += 1
            continue
        if msg.status in (
            OutboundMessage.Status.FAILED,
            OutboundMessage.Status.CANCELLED,
        ):
            counts["failed"] += 1
            continue
        log = msg.message_log
        if log is None:
            counts["pending"] += 1
            continue
        if log.status == MessageLog.Status.FAILED:
            counts["failed"] += 1
        elif log.status == MessageLog.Status.READ:
            counts["read"] += 1
        elif log.status == MessageLog.Status.DELIVERED:
            counts["delivered"] += 1
        elif log.status == MessageLog.Status.SENT:
            counts["sent"] += 1
        else:
            counts["pending"] += 1
    return counts
