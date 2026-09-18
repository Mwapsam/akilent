"""CRM actions, registered into the shared Action Registry (``apps.core.actions``)."""
from __future__ import annotations

from apps.core.actions import Action, ActionError, register


class CreateLeadAction(Action):
    name = "create_lead"

    def input_schema(self) -> dict:
        return {"required": ["account", "contact"], "optional": ["source", "owner"]}

    def execute(self, context: dict, *, account, contact, source: str = "", owner=None) -> dict:
        from apps.crm.services import create_lead

        lead = create_lead(account, contact, source=source, owner=owner)
        return {"lead_id": lead.public_id}


class CreateDealAction(Action):
    name = "create_deal"

    def input_schema(self) -> dict:
        return {"required": ["lead"], "optional": ["title", "value", "pipeline"]}

    def execute(self, context: dict, *, lead, title: str | None = None, value=0, pipeline=None) -> dict:
        from apps.crm.services import convert_lead_to_deal

        deal = convert_lead_to_deal(lead, title=title, value=value, pipeline=pipeline)
        return {"deal_id": deal.public_id}


class ChangeDealStageAction(Action):
    name = "change_deal_stage"

    def input_schema(self) -> dict:
        return {"required": ["deal", "stage"]}

    def execute(self, context: dict, *, deal, stage) -> dict:
        from apps.crm.services import move_deal_stage

        if stage.pipeline_id != deal.pipeline_id:
            raise ActionError("stage must belong to the deal's pipeline")
        deal = move_deal_stage(deal, stage)
        return {"deal_id": deal.public_id, "stage": stage.name, "status": deal.status}


register(CreateLeadAction())
register(CreateDealAction())
register(ChangeDealStageAction())
