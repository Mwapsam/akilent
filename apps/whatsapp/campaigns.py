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


_Camp = WhatsAppCampaign.Status


def _sendable_template(account, template_id) -> MessageTemplate:
    template = MessageTemplate.objects.filter(pk=template_id, account=account).first()
    if template is None:
        raise CampaignError("Choose a template.")
    _check_sendable(template)
    return template


def _check_sendable(template) -> None:
    if template.approval_status != MessageTemplate.ApprovalStatus.APPROVED:
        raise CampaignError(
            "This template isn't approved by Meta yet — only an approved template can be sent as a campaign."
        )
    if template.header_format != MessageTemplate.HeaderFormat.TEXT:
        raise CampaignError(
            "This template has a picture, video or document at the top, which "
            "campaigns can't send yet. Choose a template with a text header or none."
        )


def audience_contacts(account, *, contact_list=None, segment=None):
    """The Contact queryset an audience (one list or one segment) reaches now."""
    if segment is not None:
        from apps.contacts import segments

        try:
            return segments.contacts_for(segment.definition or {}, account)
        except segments.SegmentError as exc:
            raise CampaignError(
                f"The segment “{segment.name}” can't be used: {exc}. Edit it and try again."
            ) from exc
    if contact_list is not None:
        return contact_list.contacts.filter(account=account)
    raise CampaignError("Choose who to send to.")


def audience_contact_ids(campaign) -> list[int]:
    """Who a campaign goes to, decided when it starts (not when it was drafted)."""
    account = campaign.account
    if campaign.retry_of_id:
        opted_out = WhatsAppContact.objects.filter(
            account=account,
            contact_id__isnull=False,
            opt_in_status=WhatsAppContact.OptInStatus.OPTED_OUT,
        ).values("contact_id")
        return list(
            WhatsAppCampaignRecipient.objects.filter(
                campaign_id=campaign.retry_of_id, status=_Status.FAILED
            )
            .exclude(contact_id__in=opted_out)
            .order_by("contact_id")
            .values_list("contact_id", flat=True)
        )
    qs = audience_contacts(
        account, contact_list=campaign.contact_list, segment=campaign.segment
    )
    return list(qs.order_by("id").values_list("id", flat=True))


def _check_audience_ids(
    account, contact_ids: list, *, retry: bool = False, segment: bool = False
) -> None:
    if not contact_ids:
        if retry:
            message = (
                "Nobody is left to send to — every customer whose message failed "
                "has since opted out."
            )
        elif segment:
            message = "Nobody matches this segment right now."
        else:
            message = "This customer list is empty. Add people to it first."
        raise CampaignError(message)
    from apps.billing import api as billing_api

    if not billing_api.check_rule(
        account, "whatsapp_campaign_recipients", len(contact_ids)
    ):
        cap = billing_api.limit(account, "whatsapp_campaign_recipients")
        raise CampaignError(
            f"This audience has {len(contact_ids):,} customers, but your plan sends a WhatsApp campaign to "
            f"at most {cap:,}. Choose a smaller list or upgrade your plan."
        )


def _materialize(campaign, contact_ids: list, user=None) -> None:
    """Create the recipient rows and hand the campaign to the fan-out task.

    Called inside the caller's transaction; the first chunk is enqueued only
    once it commits, so a rollback never sends to a partial audience.
    """
    from django.utils import timezone

    WhatsAppCampaignRecipient.objects.bulk_create(
        [
            WhatsAppCampaignRecipient(campaign=campaign, contact_id=cid)
            for cid in contact_ids
        ],
        batch_size=1000,
    )
    from apps.whatsapp.delivery import send_plan_for

    campaign.recipient_count = len(contact_ids)
    campaign.status = _Camp.QUEUED
    campaign.audience_snapshot_at = timezone.now()
    campaign.send_plan = send_plan_for(campaign.account, len(contact_ids))
    campaign.record("sent", user, save=False, recipients=len(contact_ids))
    campaign.save(
        update_fields=[
            "recipient_count",
            "status",
            "audience_snapshot_at",
            "history",
            "scheduled_at",
            "submission_key",
            "send_plan",
        ]
    )
    campaign_id = campaign.pk
    transaction.on_commit(lambda: send_campaign.delay(campaign_id))


def _write_row(
    *,
    account,
    campaign,
    name,
    template,
    contact_list,
    segment,
    variable_mapping,
    variable_fallbacks,
    created_by,
    submission_key="",
) -> WhatsAppCampaign:
    if campaign is None:
        campaign = WhatsAppCampaign(account=account, created_by=created_by)
    campaign.name = (name or "").strip()[:150] or template.name
    campaign.template = template
    campaign.contact_list = contact_list if segment is None else None
    campaign.segment = segment
    campaign.variable_mapping = variable_mapping or {}
    campaign.variable_fallbacks = variable_fallbacks or {}
    if submission_key:
        campaign.submission_key = submission_key
    campaign.save()
    return campaign


def _editable(account, draft) -> WhatsAppCampaign | None:
    if draft is None:
        return None
    campaign = (
        WhatsAppCampaign.objects.select_for_update()
        .filter(pk=draft.pk, account=account)
        .first()
    )
    if campaign is None or campaign.status not in WhatsAppCampaign.EDITABLE:
        raise CampaignError(
            "This campaign has already been sent, so it can't be changed. "
            "Duplicate it to send it again."
        )
    return campaign


def create_and_queue_campaign(
    *,
    account,
    name: str,
    template_id: int,
    contact_list=None,
    segment=None,
    variable_mapping: dict | None = None,
    variable_fallbacks: dict | None = None,
    created_by=None,
    submission_key: str = "",
    send_at=None,
    tz: str = "",
    draft=None,
) -> WhatsAppCampaign:
    """Send a campaign now, or schedule it for ``send_at``.

    Sending now creates one ``WhatsAppCampaignRecipient`` per customer in the
    audience and enqueues the first fan-out chunk. Scheduling creates no
    recipients: they're chosen when it fires (``dispatch_due_campaigns``), so
    people added to the list in the meantime are included. ``draft`` turns an
    existing draft (or scheduled campaign) into this send instead of making a
    new campaign.

    Idempotent per ``submission_key``: a repeat with the same key (double
    click, browser resubmit) returns the campaign already created and sends
    nothing more.

    Raises ``CampaignError`` for anything the UI should show back to the user
    (unapproved template, empty audience, plan cap) rather than a generic 500.
    """
    from django.utils import timezone

    submission_key = (submission_key or "")[:64]
    if submission_key:
        existing = WhatsAppCampaign.objects.filter(
            account=account, submission_key=submission_key
        ).first()
        if existing is not None:
            return existing

    template = _sendable_template(account, template_id)
    if contact_list is None and segment is None:
        raise CampaignError("Choose who to send to.")
    scheduled = send_at is not None and send_at > timezone.now()

    try:
        with transaction.atomic():
            campaign = _write_row(
                account=account,
                campaign=_editable(account, draft),
                name=name,
                template=template,
                contact_list=contact_list,
                segment=segment,
                variable_mapping=variable_mapping,
                variable_fallbacks=variable_fallbacks,
                created_by=created_by,
                submission_key=submission_key,
            )
            # Checked now even when scheduling, so the owner hears about an
            # empty or over-the-plan audience while they're still here.
            contact_ids = audience_contact_ids(campaign)
            _check_audience_ids(account, contact_ids, segment=segment is not None)
            if scheduled:
                campaign.status = _Camp.SCHEDULED
                campaign.scheduled_at = send_at
                campaign.timezone = tz or ""
                campaign.record(
                    "scheduled", created_by, save=False, when=send_at.isoformat()
                )
                campaign.save(
                    update_fields=["status", "scheduled_at", "timezone", "history"]
                )
            else:
                campaign.scheduled_at = None
                _materialize(campaign, contact_ids, created_by)
    except IntegrityError:
        # A concurrent submit with the same key won the race; that one sends.
        if not submission_key:
            raise
        return WhatsAppCampaign.objects.get(
            account=account, submission_key=submission_key
        )
    return campaign


def save_draft(
    *,
    account,
    name: str,
    template_id,
    contact_list=None,
    segment=None,
    variable_mapping: dict | None = None,
    variable_fallbacks: dict | None = None,
    created_by=None,
    draft=None,
) -> WhatsAppCampaign:
    """Keep the composer's choices without sending anything.

    Anyone on the team can save a draft; only owners and admins send it (the
    views enforce that). Saving a scheduled campaign as a draft unschedules it.
    """
    template = MessageTemplate.objects.filter(pk=template_id, account=account).first()
    if template is None:
        raise CampaignError("Choose a message before saving a draft.")
    if contact_list is None and segment is None:
        raise CampaignError("Choose who to send to before saving a draft.")
    with transaction.atomic():
        existing = _editable(account, draft)
        campaign = _write_row(
            account=account,
            campaign=existing,
            name=name,
            template=template,
            contact_list=contact_list,
            segment=segment,
            variable_mapping=variable_mapping,
            variable_fallbacks=variable_fallbacks,
            created_by=created_by,
        )
        campaign.status = _Camp.DRAFT
        campaign.scheduled_at = None
        campaign.record("edited" if existing else "drafted", created_by, save=False)
        campaign.save(update_fields=["status", "scheduled_at", "history"])
    return campaign


def start_campaign(campaign_id: int, *, user=None, due_only: bool = False):
    """Send a draft or scheduled campaign now (or, ``due_only``, if its time has come).

    The recipients are chosen here, from the list/segment as it is now. A
    problem found at this point raises ``CampaignError`` for an owner pressing
    "Send now"; for a scheduled send nobody is watching, so the campaign is
    marked failed with the reason instead.
    """
    from django.utils import timezone

    with transaction.atomic():
        campaign = (
            WhatsAppCampaign.objects.select_for_update(skip_locked=due_only)
            .select_related("template", "contact_list", "segment", "account")
            .filter(pk=campaign_id)
            .first()
        )
        if campaign is None or campaign.status not in WhatsAppCampaign.EDITABLE:
            return campaign
        if due_only and (
            campaign.status != _Camp.SCHEDULED
            or campaign.scheduled_at is None
            or campaign.scheduled_at > timezone.now()
        ):
            return campaign
        try:
            _check_sendable(campaign.template)
            contact_ids = audience_contact_ids(campaign)
            _check_audience_ids(
                campaign.account,
                contact_ids,
                retry=bool(campaign.retry_of_id),
                segment=bool(campaign.segment_id),
            )
        except CampaignError as exc:
            if not due_only:
                raise
            campaign.mark_failed(str(exc))
            return campaign
        _materialize(campaign, contact_ids, user)
    return campaign


def _waiting_messages(campaign):
    return OutboundMessage.objects.filter(
        campaign_recipient__campaign=campaign,
        status__in=[OutboundMessage.Status.QUEUED, OutboundMessage.Status.HELD],
    )


def can_pause(campaign) -> bool:
    if campaign.status in WhatsAppCampaign.ACTIVE:
        return True
    # Fan-out finishes long before a big list has drained out of the send
    # queue; pausing then still holds back the messages not yet sent.
    return campaign.status == _Camp.COMPLETED and _waiting_messages(campaign).exists()


def can_cancel(campaign) -> bool:
    return campaign.status in (
        _Camp.SCHEDULED,
        _Camp.PAUSED,
        *WhatsAppCampaign.ACTIVE,
    ) or (campaign.status == _Camp.COMPLETED and _waiting_messages(campaign).exists())


def pause_campaign(campaign, *, user=None) -> bool:
    """Stop at the next chunk and hold back queued messages. False if too late."""
    if not can_pause(campaign):
        return False
    updated = WhatsAppCampaign.objects.filter(
        pk=campaign.pk, status__in=[*WhatsAppCampaign.ACTIVE, _Camp.COMPLETED]
    ).update(status=_Camp.PAUSED)
    campaign.refresh_from_db()
    if updated:
        campaign.record("paused", user)
    return bool(updated)


def resume_campaign(campaign, *, user=None) -> bool:
    with transaction.atomic():
        locked = WhatsAppCampaign.objects.select_for_update().get(pk=campaign.pk)
        if locked.status != _Camp.PAUSED:
            return False
        pending = locked.recipients.filter(status=_Status.PENDING).exists()
        if pending:
            locked.status = _Camp.SENDING if locked.started_at else _Camp.QUEUED
        else:
            locked.status = _Camp.COMPLETED
        locked.record("resumed", user, save=False)
        locked.save(update_fields=["status", "history"])
        if pending:
            campaign_id = locked.pk
            transaction.on_commit(lambda: send_campaign.delay(campaign_id))
    campaign.refresh_from_db()
    return True


def halt_campaign(campaign, error: str, *, reason=None) -> int:
    """Stop a campaign on our side (e.g. Meta paused its template): nothing more
    goes out, and it ends Failed with ``error`` shown on the detail page."""
    return _withdraw(
        campaign,
        final=_Camp.FAILED,
        reason=reason or _SkipReason.TEMPLATE_STOPPED,
        error=error,
        event="failed",
    )


def cancel_campaign(campaign, *, user=None) -> int:
    """Send nothing more. Returns how many customers won't get it now.

    Customers not reached yet are marked "not sent: cancelled"; messages still
    waiting in the send queue are cancelled. A message already being sent at
    this moment (claimed by the outbound queue) can't be called back.
    """
    if not can_cancel(campaign):
        return 0
    return _withdraw(
        campaign,
        final=_Camp.CANCELLED,
        reason=_SkipReason.CANCELLED,
        user=user,
        event="cancelled",
    )


def _withdraw(campaign, *, final, reason, event, user=None, error="") -> int:
    from django.core.cache import cache
    from django.utils import timezone

    from apps.whatsapp.models import MessageLog

    with transaction.atomic():
        locked = WhatsAppCampaign.objects.select_for_update().get(pk=campaign.pk)
        if locked.status in (_Camp.CANCELLED, _Camp.FAILED, _Camp.DRAFT):
            return 0
        not_reached = locked.recipients.filter(status=_Status.PENDING).update(
            status=_Status.SKIPPED, skip_reason=reason
        )
        waiting = [
            m
            for m in _waiting_messages(locked).values("pk", "message_log_id")
            if not cache.get(f"wa_outbound_claim:{m['pk']}")
        ]
        withdrawn_ids = [m["pk"] for m in waiting]
        withdrawn = OutboundMessage.objects.filter(
            pk__in=withdrawn_ids,
            status__in=[OutboundMessage.Status.QUEUED, OutboundMessage.Status.HELD],
        ).update(
            status=OutboundMessage.Status.CANCELLED,
            next_attempt_at=None,
            last_error=f"Not sent: {dict(_SkipReason.choices)[reason].lower()}.",
        )
        log_ids = [m["message_log_id"] for m in waiting if m["message_log_id"]]
        if log_ids:
            MessageLog.objects.filter(pk__in=log_ids).update(
                status=MessageLog.Status.FAILED
            )
        locked.recipients.filter(
            message_id__in=withdrawn_ids,
            message__status=OutboundMessage.Status.CANCELLED,
        ).update(status=_Status.SKIPPED, skip_reason=reason)
        locked.queued_count = max(locked.queued_count - withdrawn, 0)
        locked.skipped_count = locked.skipped_count + not_reached + withdrawn
        locked.status = final
        if error:
            locked.error = error[:5000]
        locked.completed_at = timezone.now()
        locked.record(event, user, save=False, not_sent=not_reached + withdrawn)
        locked.save(
            update_fields=[
                "queued_count",
                "skipped_count",
                "status",
                "error",
                "completed_at",
                "history",
            ]
        )
    campaign.refresh_from_db()
    return not_reached + withdrawn


def duplicate_campaign(campaign, *, user=None) -> WhatsAppCampaign:
    """A new draft with the same audience, message and blanks."""
    copy = WhatsAppCampaign.objects.create(
        account=campaign.account,
        name=f"Copy of {campaign.name}"[:150],
        contact_list=campaign.contact_list,
        segment=campaign.segment,
        template=campaign.template,
        variable_mapping=campaign.variable_mapping,
        variable_fallbacks=campaign.variable_fallbacks,
        status=_Camp.DRAFT,
        created_by=user,
    )
    copy.record("drafted", user, copied_from=campaign.pk)
    return copy


def failed_count(campaign) -> int:
    return campaign.recipients.filter(status=_Status.FAILED).count()


def retry_failed_campaign(campaign, *, user=None) -> WhatsAppCampaign:
    """Send again, only to the customers whose message failed in ``campaign``."""
    campaign.refresh_from_db(fields=["status"])
    if campaign.status in (*WhatsAppCampaign.EDITABLE, *WhatsAppCampaign.ACTIVE):
        raise CampaignError("Wait until this campaign has finished sending.")
    if not failed_count(campaign):
        raise CampaignError("No messages failed in this campaign.")
    _check_sendable(campaign.template)
    retry = WhatsAppCampaign.objects.create(
        account=campaign.account,
        name=f"{campaign.name} (retry)"[:150],
        contact_list=campaign.contact_list,
        segment=campaign.segment,
        template=campaign.template,
        variable_mapping=campaign.variable_mapping,
        variable_fallbacks=campaign.variable_fallbacks,
        retry_of=campaign,
        status=_Camp.DRAFT,
        created_by=user,
    )
    try:
        return start_campaign(retry.pk, user=user)
    except CampaignError:
        retry.delete()
        raise


_TEST_SENDS_PER_HOUR = 10


def send_test(*, account, template, mapping: dict, fallbacks: dict, phone: str, user):
    """Queue one copy of the message to the owner's own number. Not a campaign:
    no recipients, nothing in the campaign stats, no customer record created.

    The owner typing their own number to receive it is their consent, so a
    promotion is allowed to reach that number (recorded as such on the
    identity). A number that replied STOP stays opted out.
    """
    from types import SimpleNamespace

    from django.core.cache import cache
    from django.core.exceptions import ValidationError

    from apps.automation import variables
    from apps.automation.workflows import _build_template_payload
    from apps.whatsapp.models.contact import normalize_phone

    _check_sendable(template)
    try:
        number = normalize_phone(phone or "")
    except ValidationError:
        number = ""
    if not number:
        raise CampaignError("Enter your WhatsApp number, with the country code.")
    key = f"wa_campaign_test:{account.pk}:{user.pk}"
    cache.add(key, 0, 3600)
    try:
        if cache.incr(key) > _TEST_SENDS_PER_HOUR:
            raise CampaignError(
                "You've sent a lot of tests this hour. Try again a little later."
            )
    except ValueError:
        pass

    identity, _ = WhatsAppContact.objects.get_or_create(
        account=account, phone_number=number
    )
    if identity.opt_in_status == WhatsAppContact.OptInStatus.OPTED_OUT:
        raise CampaignError(
            "That number replied STOP to your business, so WhatsApp messages to it are blocked."
        )
    if (
        template.category == MessageTemplate.Category.MARKETING
        and identity.opt_in_status != WhatsAppContact.OptInStatus.OPTED_IN
    ):
        identity.record_opt_in(f"campaign_test:user:{user.pk}")

    me = SimpleNamespace(
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        full_name=user.get_full_name() or "",
        phone=number,
        email=user.email or "",
        attributes={},
    )
    params = {}
    for var in template.variables or []:
        try:
            params.update(
                variables.resolve(
                    [var],
                    mapping,
                    contact=me,
                    account=account,
                    context={},
                    fallbacks=fallbacks,
                )
            )
        except variables.MissingValue:
            params[var] = f"[{var}]"
    return OutboundMessage.objects.create(
        account=account,
        contact=identity,
        template=template,
        payload=_build_template_payload(template, params, sent_by="campaign_test"),
    )


@shared_task(queue="campaigns")
def dispatch_due_campaigns() -> dict:
    """Beat, every minute: start every scheduled campaign whose time has come.

    ``start_campaign(due_only=True)`` re-checks under a ``skip_locked`` row
    lock, so two overlapping beats can't both start the same campaign.
    """
    from django.utils import timezone

    due = list(
        WhatsAppCampaign.objects.filter(
            status=_Camp.SCHEDULED, scheduled_at__lte=timezone.now()
        )
        .order_by("scheduled_at")
        .values_list("pk", flat=True)[:100]
    )
    started = 0
    for pk in due:
        try:
            campaign = start_campaign(pk, due_only=True)
        except Exception:
            logger.exception("dispatch_due_campaigns: campaign %s", pk)
            continue
        if campaign is not None and campaign.status == _Camp.QUEUED:
            started += 1
    if started:
        logger.info("dispatch_due_campaigns: started=%s", started)
    return {"due": len(due), "started": started}


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
    from django.core.cache import cache

    from apps.automation import variables
    from apps.automation.workflows import _build_template_payload
    from apps.core.channels import channel_state

    state = channel_state("whatsapp")
    chain_key = f"wa_campaign_waiting:{campaign_id}"
    if state != "enabled":
        # Wait until WhatsApp is back on (or settings load). The campaign keeps its status
        # and resumes from where it stopped, since this function is restart-safe.
        # Only one chain waits per campaign: the sweeper and a resume would otherwise
        # each start another, and they'd all pile up retrying for days.
        countdown = 900 if state == "disabled" else 300
        task_id = getattr(self.request, "id", None) or "inline"
        if not cache.add(chain_key, task_id, countdown * 2):
            if cache.get(chain_key) != task_id:
                return
            cache.set(chain_key, task_id, countdown * 2)
        raise self.retry(countdown=countdown, max_retries=10_000)
    cache.delete(chain_key)

    campaign = WhatsAppCampaign.objects.select_related("template", "contact_list").get(
        pk=campaign_id
    )
    # Finished, paused, cancelled, or not started yet (draft / scheduled): no fan-out.
    # Resuming or starting dispatches this task again.
    if campaign.status not in WhatsAppCampaign.ACTIVE:
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


def _unreachable_numbers(phones: list) -> set:
    from apps.contacts.models import ContactPhone

    if not phones:
        return set()
    return set(
        ContactPhone.objects.filter(
            normalized_value__in=phones,
            whatsapp_status=ContactPhone.WhatsAppStatus.UNREACHABLE,
        ).values_list("normalized_value", flat=True)
    )


def _kick_drain() -> None:
    try:
        from apps.whatsapp.tasks import drain_outbound_queue

        drain_outbound_queue.delay()
    except Exception:  # the beat drain picks them up anyway
        logger.warning("could not kick the outbound drain", exc_info=True)


def _process_chunk(campaign, variables, build_payload) -> bool:
    """Process one chunk of PENDING recipients; return True when none remain."""
    is_marketing = campaign.template.category == MessageTemplate.Category.MARKETING

    with transaction.atomic():
        # Under the campaign's row lock, so a pause or cancel lands between
        # chunks, never halfway through one.
        status = (
            WhatsAppCampaign.objects.select_for_update()
            .filter(pk=campaign.pk)
            .values_list("status", flat=True)
            .first()
        )
        if status not in WhatsAppCampaign.ACTIVE:
            return True
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
        unreachable = _unreachable_numbers(
            [wc.phone_number for wc in wa_by_contact.values()]
        )

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
            if wc.phone_number in unreachable:
                # WhatsApp already told us this number isn't on WhatsApp.
                to_update.append(_skip(r, _SkipReason.NOT_ON_WHATSAPP))
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
        from django.utils import timezone

        from apps.whatsapp import delivery

        plan = campaign.send_plan or {}
        now = timezone.now()
        zone = delivery._zone(campaign.account) if plan.get("window") else None
        window = delivery.send_window(campaign.account) if plan.get("window") else None
        first_index = campaign.queued_count
        outbound_rows = [
            OutboundMessage(
                account=campaign.account,
                contact=wc,
                template=campaign.template,
                payload=build_payload(campaign.template, params, sent_by="campaign"),
                idempotency_key=f"campaign:{campaign.id}:recipient:{r.id}",
                priority=OutboundMessage.PRIORITY_BULK,
                scheduled_at=delivery.scheduled_for(
                    campaign.account,
                    plan,
                    first_index + i,
                    now,
                    zone=zone,
                    window=window,
                )
                if plan
                else now,
            )
            for i, (r, wc, params) in enumerate(to_create)
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
        logger.info(
            "campaign_chunk campaign=%s account=%s claimed=%s queued=%s skipped=%s",
            campaign.pk,
            campaign.account_id,
            len(chunk),
            len(to_create),
            len(to_update) - len(to_create),
        )
        if to_create:
            # Start sending now rather than waiting for the next 10s drain beat.
            transaction.on_commit(_kick_drain)
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


def recipients_with_outcome(campaign: WhatsAppCampaign):
    """Every recipient with ``outcome``: not_sent · waiting · or its delivery bucket."""
    from django.db.models import Case, CharField, Value, When

    return (
        campaign.recipients.select_related("contact")
        .annotate(
            outcome=Case(
                When(status=_Status.SKIPPED, then=Value("not_sent")),
                When(status=_Status.PENDING, then=Value("waiting")),
                default=_delivery_bucket(),
                output_field=CharField(),
            )
        )
        .order_by("pk")
    )


def campaign_delivery_stats(campaign: WhatsAppCampaign) -> dict:
    """What happened to every message this campaign handed to the send queue."""
    return delivery_stats_for([campaign.pk])[campaign.pk]


_HOLD_MAX = 72  # hours a campaign message may wait for plan room before it's dropped
_FAILURE_ALERT_RATE = 0.2
_FAILURE_ALERT_MIN = 50


@shared_task(queue="campaigns")
def cancel_stale_campaign_holds() -> dict:
    """Hourly: a campaign message held by the plan's monthly limit for more than
    ``WHATSAPP_CAMPAIGN_HOLD_MAX_HOURS`` (72) is cancelled, not sent days late.
    The recipient is shown as "not sent — plan limit" on the campaign page."""
    from datetime import timedelta

    from django.conf import settings
    from django.db.models import F
    from django.utils import timezone

    hours = int(getattr(settings, "WHATSAPP_CAMPAIGN_HOLD_MAX_HOURS", _HOLD_MAX))
    cutoff = timezone.now() - timedelta(hours=hours)
    stale = list(
        OutboundMessage.objects.filter(
            status=OutboundMessage.Status.HELD,
            created_at__lt=cutoff,
            campaign_recipient__isnull=False,
        ).values_list("pk", "campaign_recipient__campaign_id")
    )
    if not stale:
        return {"cancelled": 0}
    by_campaign: dict = {}
    for pk, cid in stale:
        by_campaign.setdefault(cid, []).append(pk)
    total = 0
    for cid, ids in by_campaign.items():
        with transaction.atomic():
            n = OutboundMessage.objects.filter(
                pk__in=ids, status=OutboundMessage.Status.HELD
            ).update(
                status=OutboundMessage.Status.CANCELLED,
                next_attempt_at=None,
                last_error="Not sent: the plan's monthly limit stayed full, so it wasn't sent days late.",
            )
            WhatsAppCampaignRecipient.objects.filter(
                message_id__in=ids, message__status=OutboundMessage.Status.CANCELLED
            ).update(status=_Status.SKIPPED, skip_reason=_SkipReason.PLAN_LIMIT)
            WhatsAppCampaign.objects.filter(pk=cid).update(
                queued_count=F("queued_count") - n, skipped_count=F("skipped_count") + n
            )
        total += n
    logger.warning("cancel_stale_campaign_holds: cancelled=%s", total)
    return {"cancelled": total}


@shared_task(queue="campaigns")
def alert_on_campaign_failure_rate() -> dict:
    """Every 15 minutes: tell the owners once when a recent campaign's messages
    are failing at a high rate (a number problem, a bad list, a blocked template)."""
    from datetime import timedelta

    from django.utils import timezone

    alerted = 0
    recent = WhatsAppCampaign.objects.filter(
        started_at__gte=timezone.now() - timedelta(days=2)
    ).exclude(status__in=[_Camp.DRAFT, _Camp.SCHEDULED])
    for campaign in recent:
        if any(e.get("event") == "failure_alert" for e in campaign.history or []):
            continue
        stats = campaign_delivery_stats(campaign)
        settled = stats["failed"] + stats["sent"] + stats["delivered"] + stats["read"]
        if (
            settled < _FAILURE_ALERT_MIN
            or stats["failed"] / settled < _FAILURE_ALERT_RATE
        ):
            continue
        try:
            from django.urls import reverse

            from apps.accounts.notifications import notify_team

            notify_team(
                campaign.account,
                to="owners",
                subject=f"Many messages in “{campaign.name}” are failing",
                text=(
                    f"{stats['failed']} of {settled} messages in your WhatsApp campaign "
                    f"“{campaign.name}” failed. Open the campaign to see why — often the "
                    "numbers aren't on WhatsApp or your number has a problem."
                ),
                path=reverse("whatsapp-campaign-detail", args=[campaign.pk]),
                reason="You're getting this because a campaign's failure rate is high.",
            )
        except Exception:
            logger.exception("campaign failure alert failed for %s", campaign.pk)
            continue
        campaign.record("failure_alert")
        alerted += 1
    return {"alerted": alerted}


def stop_campaigns_for_template(template_ids) -> int:
    """Meta paused or rejected a template: stop every campaign still sending it."""
    stopped = 0
    for campaign in WhatsAppCampaign.objects.filter(
        template_id__in=list(template_ids),
        status__in=[*WhatsAppCampaign.ACTIVE, _Camp.PAUSED, _Camp.COMPLETED],
    ):
        if (
            campaign.status == _Camp.COMPLETED
            and not _waiting_messages(campaign).exists()
        ):
            continue
        halt_campaign(
            campaign,
            "Meta paused or rejected this campaign's template while it was sending, "
            "so the rest wasn't sent. Messages already sent still arrive.",
        )
        stopped += 1
    return stopped
