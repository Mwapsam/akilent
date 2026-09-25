"""Thin-CRM service layer: create/convert/move, emitting durable Events and
enrolling Workflows — mirrors ``apps.conversations.services``'s pattern so
CRM and the operational spine stay consistent.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.conversations.services import emit_event
from apps.crm.models import Deal, Lead, Pipeline, Stage

logger = logging.getLogger(__name__)


_OPEN_LEAD_STATUSES = [Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED]


def create_lead(account, contact, *, source: str = "", owner=None, conversation_id: str = "") -> Lead:
    """Create a Lead for ``contact``, or return its existing open one.

    A contact should have at most one open opportunity being tracked at a
    time — without this, a trigger like "every inbound message creates a
    lead" would spawn a new Lead per message from the same returning
    customer. Callers that genuinely need a second concurrent lead (rare)
    should create one directly via ``Lead.objects.create``.
    """
    existing = Lead.objects.filter(account=account, contact=contact, status__in=_OPEN_LEAD_STATUSES).first()
    if existing is not None:
        return existing

    from apps.conversations.attribution import resolve_conversation

    lead = Lead.objects.create(
        account=account, contact=contact, source=source, owner=owner,
        conversation=resolve_conversation(account, contact, public_id=conversation_id),
    )

    emit_event(
        account=account, type="lead.created", occurred_at=lead.created_at,
        source="crm", subject_type="lead", subject_id=lead.public_id,
        payload={"contact_id": contact.public_id, "source": source},
    )
    _dispatch_legacy_lead_created(account, lead)
    # A lead opened from a conversation carries it, so a workflow can act in that thread
    # (assign it, tell the team) rather than guessing which conversation was meant.
    context = {"lead_id": lead.public_id, **({"conversation_id": conversation_id} if conversation_id else {})}
    _enroll_workflows(account, "lead.created", contact, context)
    return lead


# Statuses a person or automation can put a lead in. "converted" is reached only by turning the
# lead into a deal (convert_lead_to_deal), which is what creates the deal it points at.
SETTABLE_LEAD_STATUSES = (Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED, Lead.Status.LOST)


def set_lead_status(lead: Lead, status: str) -> bool:
    """Move a lead to ``status``. Returns True if it changed (a repeat is a quiet no-op).

    Emits a durable event and starts workflows on ``lead.status_changed``, plus
    ``lead.qualified`` / ``lead.lost`` for the two moments owners most often act on.
    """
    if status not in SETTABLE_LEAD_STATUSES:
        raise ValueError("Choose new, contacted, qualified or lost.")
    if lead.status == Lead.Status.CONVERTED:
        raise ValueError("This customer is already in your pipeline. Move their deal instead.")
    if lead.status == status:
        return False

    previous = lead.status
    lead.status = status
    lead.save(update_fields=["status", "updated_at"])

    emit_event(
        account=lead.account, type="lead.status_changed", occurred_at=timezone.now(),
        source="crm", subject_type="lead", subject_id=lead.public_id,
        payload={"contact_id": lead.contact.public_id, "from": previous, "to": status},
    )
    context = {"lead_id": lead.public_id, "status": status, "previous_status": previous}
    _enroll_workflows(lead.account, "lead.status_changed", lead.contact, context)
    if status == Lead.Status.QUALIFIED:
        _enroll_workflows(lead.account, "lead.qualified", lead.contact, context)
    elif status == Lead.Status.LOST:
        _enroll_workflows(lead.account, "lead.lost", lead.contact, context)
    return True


def convert_lead_to_deal(lead: Lead, *, title: str | None = None, value=0,
                          pipeline: Pipeline | None = None) -> Deal:
    """Convert a Lead into a Deal in its first (non-terminal) stage."""
    if lead.status == Lead.Status.CONVERTED:
        raise ValueError(f"lead {lead.pk} is already converted")

    from apps.conversations.attribution import resolve_conversation

    pipeline = pipeline or Pipeline.ensure_default(lead.account)
    first_stage = pipeline.stages.filter(is_won=False, is_lost=False).order_by("order").first()
    if first_stage is None:
        raise ValueError(f"pipeline {pipeline.pk} has no open stage to place a new deal in")

    with transaction.atomic():
        deal = Deal.objects.create(
            account=lead.account, contact=lead.contact, pipeline=pipeline, stage=first_stage,
            title=title or f"{lead.contact}", value=value,
            conversation=lead.conversation or resolve_conversation(lead.account, lead.contact),
        )
        lead.status = Lead.Status.CONVERTED
        lead.converted_at = timezone.now()
        lead.converted_to_deal = deal
        lead.save(update_fields=["status", "converted_at", "converted_to_deal", "updated_at"])

    emit_event(
        account=deal.account, type="deal.created", occurred_at=deal.created_at,
        source="crm", subject_type="deal", subject_id=deal.public_id,
        payload={"contact_id": deal.contact.public_id, "lead_id": lead.public_id, "stage": first_stage.name},
    )
    _enroll_workflows(deal.account, "deal.created", deal.contact, {"deal_id": deal.public_id})
    return deal


def move_deal_stage(deal: Deal, stage: Stage) -> Deal:
    if stage.pipeline_id != deal.pipeline_id:
        raise ValueError("stage must belong to the deal's pipeline")

    previous_stage = deal.stage
    deal.stage = stage
    if stage.is_won:
        deal.status = Deal.Status.WON
        deal.closed_at = timezone.now()
    elif stage.is_lost:
        deal.status = Deal.Status.LOST
        deal.closed_at = timezone.now()
    else:
        deal.status = Deal.Status.OPEN
        deal.closed_at = None
    deal.save(update_fields=["stage", "status", "closed_at", "updated_at"])

    emit_event(
        account=deal.account, type="deal.stage_changed", occurred_at=timezone.now(),
        source="crm", subject_type="deal", subject_id=deal.public_id,
        payload={
            "contact_id": deal.contact.public_id,
            "from_stage": previous_stage.name, "to_stage": stage.name,
            "status": deal.status,
        },
    )
    _dispatch_legacy_deal_stage_changed(deal.account, deal, stage)
    _enroll_workflows(deal.account, "deal.stage_changed", deal.contact, {
        "deal_id": deal.public_id, "stage": stage.name, "status": deal.status,
    })
    return deal


def _dispatch_legacy_lead_created(account, lead: Lead) -> None:
    """Best-effort bridge to the legacy AutomationRule trigger of the same name."""
    try:
        from apps.automation.triggers import on_lead_created

        on_lead_created(account.id, lead.public_id, {"contact_id": lead.contact.public_id})
    except Exception:
        logger.exception("_dispatch_legacy_lead_created failed for lead=%s", lead.pk)


def _dispatch_legacy_deal_stage_changed(account, deal: Deal, stage: Stage) -> None:
    try:
        from apps.automation.triggers import on_deal_stage_changed

        on_deal_stage_changed(account.id, deal.public_id, stage.name)
    except Exception:
        logger.exception("_dispatch_legacy_deal_stage_changed failed for deal=%s", deal.pk)


def _enroll_workflows(account, trigger_type: str, contact, context: dict) -> None:
    try:
        from apps.automation.workflow_engine import enroll_for_trigger

        enroll_for_trigger(account.id, trigger_type, contact, context=context)
    except Exception:
        logger.exception("_enroll_workflows failed for trigger=%s", trigger_type)
