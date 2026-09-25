"""CRM actions, registered into the shared Action Registry (``apps.core.actions``)."""
from __future__ import annotations

from apps.core.actions import Action, ActionError, register


class CreateLeadAction(Action):
    name = "create_lead"
    module = "crm"
    scope_kwarg = "account"

    def input_schema(self) -> dict:
        return {"required": ["account", "contact"], "optional": ["source", "owner"]}

    def execute(self, context: dict, *, account, contact, source: str = "", owner=None) -> dict:
        from apps.crm.services import create_lead

        lead = create_lead(account, contact, source=source, owner=owner)
        return {"lead_id": lead.public_id}


class CaptureConversationLeadAction(Action):
    """Open a lead for a customer whose message showed buying interest.

    Distinct from ``create_lead`` in one way that matters: it's idempotent per
    customer. It's called on every inbound message, so a customer asking three
    questions must stay one opportunity. Returns the existing lead's id (and
    ``created: False``) instead of raising, since "already a lead" is the
    normal case, not an error.
    """

    name = "capture_conversation_lead"
    module = "crm"
    scope_kwarg = "account"

    def input_schema(self) -> dict:
        return {
            "required": ["account", "contact"],
            "optional": ["conversation_id", "signal", "owner"],
        }

    def execute(self, context: dict, *, account, contact, conversation_id: str = "",
                signal: str = "", owner=None) -> dict:
        from apps.crm.models import Lead
        from apps.crm.services import create_lead

        existing = Lead.objects.filter(
            account=account, contact=contact,
            status__in=[Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED],
        ).first()
        if existing is not None:
            return {"lead_id": existing.public_id, "created": False}

        lead = create_lead(
            account, contact, source="conversation", owner=owner, conversation_id=conversation_id)
        # Record what the customer said that opened this, on the timeline the
        # customer page already renders: an owner who sees an unexpected lead
        # has to be able to tell why it exists.
        from apps.contacts.services import record_contact_event

        record_contact_event(contact, "lead.auto_created", data={
            "signal": signal, "conversation_id": conversation_id, "lead_id": lead.public_id,
        })
        return {"lead_id": lead.public_id, "created": True}


class UpdateLeadStatusAction(Action):
    """Mark a lead new, contacted, qualified or lost (idempotent)."""

    name = "update_lead_status"
    module = "crm"
    scope_kwarg = "lead"

    def input_schema(self) -> dict:
        return {"required": ["lead", "status"]}

    def execute(self, context: dict, *, lead, status: str) -> dict:
        from apps.crm.services import set_lead_status

        try:
            changed = set_lead_status(lead, status)
        except ValueError as exc:
            raise ActionError(str(exc)) from exc
        return {"lead_id": lead.public_id, "status": lead.status, "changed": changed}


class CreateDealAction(Action):
    name = "create_deal"
    module = "crm"
    scope_kwarg = "lead"

    def input_schema(self) -> dict:
        return {"required": ["lead"], "optional": ["title", "value", "pipeline"]}

    def execute(self, context: dict, *, lead, title: str | None = None, value=0, pipeline=None) -> dict:
        from apps.crm.services import convert_lead_to_deal

        deal = convert_lead_to_deal(lead, title=title, value=value, pipeline=pipeline)
        return {"deal_id": deal.public_id}


class ChangeDealStageAction(Action):
    name = "change_deal_stage"
    module = "crm"
    scope_kwarg = "deal"

    def input_schema(self) -> dict:
        return {"required": ["deal", "stage"]}

    def execute(self, context: dict, *, deal, stage) -> dict:
        from apps.crm.services import move_deal_stage

        if stage.pipeline_id != deal.pipeline_id:
            raise ActionError("stage must belong to the deal's pipeline")
        deal = move_deal_stage(deal, stage)
        return {"deal_id": deal.public_id, "stage": stage.name, "status": deal.status}


register(CreateLeadAction())
register(CaptureConversationLeadAction())
register(UpdateLeadStatusAction())
register(CreateDealAction())
register(ChangeDealStageAction())
