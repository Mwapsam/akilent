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

from django.db import IntegrityError, transaction

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
    submission_key: str = "",
) -> WhatsAppCampaign:
    """Create a ``WhatsAppCampaign`` + one ``WhatsAppCampaignRecipient`` per
    contact, and enqueue the first fan-out chunk.

    Idempotent per ``submission_key``: a repeat with the same key (double
    click, browser resubmit) returns the campaign already created and sends
    nothing more.

    Raises ``CampaignError`` for anything the UI should show back to the user
    (no approved template, empty list) rather than a generic 500. Recipient
    rows are all ``PENDING`` — nothing is sent inline here, so this returns
    fast regardless of list size.
    """
    submission_key = (submission_key or "")[:64]
    if submission_key:
        existing = WhatsAppCampaign.objects.filter(
            account=account, submission_key=submission_key
        ).first()
        if existing is not None:
            return existing

    template = MessageTemplate.objects.filter(pk=template_id, account=account).first()
    if template is None:
        raise CampaignError("Choose a template.")
    if template.approval_status != MessageTemplate.ApprovalStatus.APPROVED:
        raise CampaignError(
            "This template isn't approved by Meta yet — only an approved template can be sent as a campaign."
        )
    if template.header_format != MessageTemplate.HeaderFormat.TEXT:
        raise CampaignError(
            "This template has a picture, video or document at the top, which "
            "campaigns can't send yet. Choose a template with a text header or none."
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

    # One unit: a failure partway through the recipient rows must not leave a
    # QUEUED campaign with only some of its audience (the sweeper would send it).
    try:
        with transaction.atomic():
            campaign = _create_campaign_rows(
                account=account,
                name=name,
                contact_list=contact_list,
                template=template,
                contact_ids=contact_ids,
                variable_mapping=variable_mapping,
                variable_fallbacks=variable_fallbacks,
                created_by=created_by,
                submission_key=submission_key,
            )
            campaign_id = campaign.id
            transaction.on_commit(lambda: send_campaign.delay(campaign_id))
    except IntegrityError:
        # A concurrent submit with the same key won the race; that one sends.
        if not submission_key:
            raise
        return WhatsAppCampaign.objects.get(
            account=account, submission_key=submission_key
        )
    return campaign


def _create_campaign_rows(
    *,
    account,
    name,
    contact_list,
    template,
    contact_ids,
    variable_mapping,
    variable_fallbacks,
    created_by,
    submission_key,
) -> WhatsAppCampaign:
    campaign = WhatsAppCampaign.objects.create(
        submission_key=submission_key,
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
    from apps.core.channels import channel_state

    state = channel_state("whatsapp")
    if state != "enabled":
        # Wait until WhatsApp is back on (or settings load). The campaign keeps its status
        # and resumes from where it stopped, since this function is restart-safe.
        raise self.retry(
            countdown=900 if state == "disabled" else 300, max_retries=10_000
        )

    campaign = WhatsAppCampaign.objects.select_related("template", "contact_list").get(
        pk=campaign_id
    )
    if campaign.status in (
        WhatsAppCampaign.Status.COMPLETED,
        WhatsAppCampaign.Status.FAILED,
    ):
        return
    campaign.mark_sending()

    try:
        done = _process_chunk(campaign, variables, _build_template_payload)
    except Exception as exc:
        # Anything unexpected here (a template Meta can't take, bad data) would
        # otherwise leave the campaign "Sending" forever: the chunk rolled back,
        # but mark_sending() above had already committed. A transient error gets
        # the task's retries first; after that — or straight away for bad
        # template/data (ValueError) — fail it loudly, with the reason the
        # detail page shows the owner.
        if not isinstance(exc, ValueError) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc) from exc
        logger.exception("send_campaign: campaign %s failed", campaign_id)
        campaign.refresh_from_db()
        campaign.mark_failed(_failure_message(exc))
        return

    if not done:
        transaction.on_commit(lambda: send_campaign.delay(campaign_id))


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, ValueError) and str(exc):
        return str(exc)
    return (
        "Something went wrong while preparing this campaign's messages, so it was "
        "stopped. Nothing more will be sent. Try again, or contact support if it "
        "happens again."
    )


def _pick_identities(campaign, chunk) -> dict:
    """The WhatsAppContact each recipient in *chunk* will be sent to.

    One query for the chunk. A contact with more than one identity (their phone
    changed) is sent to the one matching their current phone. A contact with a
    phone but no identity yet — added before identities were created for every
    customer — gets one now, so they're not skipped as "no WhatsApp number".
    """
    from apps.whatsapp.services.contacts import ensure_whatsapp_identity

    by_contact: dict = {}
    phones = {r.contact_id: r.contact.phone for r in chunk}
    for wc in WhatsAppContact.objects.filter(
        account=campaign.account, contact_id__in=list(phones)
    ).order_by("pk"):
        current = by_contact.get(wc.contact_id)
        if current is None or wc.phone_number == phones.get(wc.contact_id):
            by_contact[wc.contact_id] = wc
    for r in chunk:
        if r.contact_id not in by_contact and r.contact.phone:
            wc = ensure_whatsapp_identity(r.contact)
            if wc is not None:
                by_contact[r.contact_id] = wc
    return by_contact


def _process_chunk(campaign, variables, build_payload) -> bool:
    """Process one chunk of PENDING recipients; return True when none remain."""
    is_marketing = campaign.template.category == MessageTemplate.Category.MARKETING

    with transaction.atomic():
        chunk = list(
            WhatsAppCampaignRecipient.objects.select_for_update()
            .filter(campaign=campaign, status=_Status.PENDING)
            .select_related("contact")
            .order_by("pk")[:_CAMPAIGN_CHUNK_SIZE]
        )

        if not chunk:
            return not WhatsAppCampaignRecipient.objects.filter(
                campaign=campaign, status=_Status.PENDING
            ).exists()

        # Record progress before doing the work: if this chunk fails partway
        # through, the atomic block rolls this back too, so a stuck sweeper
        # re-check still sees the last *successful* chunk's timestamp.
        campaign.touch_progress()

        wa_by_contact = _pick_identities(campaign, chunk)

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
            if (
                is_marketing
                and wc.opt_in_status != WhatsAppContact.OptInStatus.OPTED_IN
            ):
                # Would only be refused one by one at send time (_authorize_send);
                # say so up front instead, as a reason the owner can act on.
                to_update.append(_skip(r, _SkipReason.NOT_OPTED_IN))
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

        # Build OutboundMessage rows via the shared payload helper and bulk_create
        # them, instead of N calls into send_whatsapp_message.
        #
        # idempotency_key is deterministic (campaign+recipient), not a random
        # uuid: the DB's partial unique constraint on (account, idempotency_key)
        # is a second, independent guard if a recipient is ever handed to this
        # branch twice.
        outbound_rows = [
            OutboundMessage(
                account=campaign.account,
                contact=wc,
                template=campaign.template,
                payload=build_payload(campaign.template, params, sent_by="campaign"),
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
        return done


DELIVERY_BUCKETS = (
    "sent",
    "delivered",
    "read",
    "failed",
    "held",
    "unconfirmed",
    "pending",
)


def _delivery_bucket():
    """Which delivery bucket a QUEUED/FAILED recipient falls in, in precedence order:

    failed (recipient or message failed/cancelled, or Meta reported failure) ·
    held (plan limit reached; retried automatically) · unconfirmed (may not have
    been sent; never resent) · read · delivered · sent · pending (still queued).
    ``OutboundMessage.status`` is authoritative for terminal failure, not
    ``message_log.status``, so a stale "delivered" log can never mask it.
    """
    from django.db.models import Case, CharField, Value, When

    from apps.whatsapp.models import MessageLog

    _Out = OutboundMessage.Status
    _Log = MessageLog.Status
    return Case(
        When(status=_Status.FAILED, then=Value("failed")),
        When(message__isnull=True, then=Value("pending")),
        When(message__status__in=[_Out.FAILED, _Out.CANCELLED], then=Value("failed")),
        When(message__status=_Out.HELD, then=Value("held")),
        When(message__status=_Out.UNCONFIRMED, then=Value("unconfirmed")),
        When(message__message_log__status=_Log.FAILED, then=Value("failed")),
        When(message__message_log__status=_Log.READ, then=Value("read")),
        When(message__message_log__status=_Log.DELIVERED, then=Value("delivered")),
        When(message__message_log__status=_Log.SENT, then=Value("sent")),
        default=Value("pending"),
        output_field=CharField(),
    )


def delivery_stats_for(campaign_ids) -> dict:
    """``{campaign_id: {bucket: n}}`` for several campaigns in one grouped query.

    Counts recipients that were QUEUED (a message was created) or have since
    FAILED (the send pipeline gave up on that message — see
    apps.whatsapp.tasks._notify_terminal_failure). Recipients skipped before a
    message existed have no delivery outcome; see the skip breakdown instead.
    Costs the same for 50 or 50,000 recipients.
    """
    from django.db.models import Count, Q

    ids = list(campaign_ids)
    out = {cid: dict.fromkeys(DELIVERY_BUCKETS, 0) for cid in ids}
    if not ids:
        return out
    rows = (
        WhatsAppCampaignRecipient.objects.filter(
            Q(status=_Status.QUEUED) | Q(status=_Status.FAILED), campaign_id__in=ids
        )
        .annotate(bucket=_delivery_bucket())
        .values("campaign_id", "bucket")
        .annotate(n=Count("id"))
    )
    for row in rows:
        out[row["campaign_id"]][row["bucket"]] = row["n"]
    return out


def campaign_delivery_stats(campaign: WhatsAppCampaign) -> dict:
    """What happened to every message this campaign handed to the send queue."""
    return delivery_stats_for([campaign.pk])[campaign.pk]
