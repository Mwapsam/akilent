"""Management command: backfill StageTransition rows from existing Event records.

Run idempotently: rows already present (matched by source_event_id) are skipped.
Also creates estimated creation rows for leads/deals that still have none.
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill StageTransition rows from lead.status_changed and deal.stage_changed events"

    def handle(self, *args, **options):
        from apps.conversations.models import Event
        from apps.crm.models import Deal, Lead, StageTransition

        # ---- 1. Backfill from lead.status_changed events ----
        lead_events = Event.objects.filter(type="lead.status_changed").select_related(
            "account"
        )
        created_lead = 0
        skipped_lead = 0
        for ev in lead_events:
            src = f"evt:{ev.public_id}"
            if StageTransition.objects.filter(source_event_id=src).exists():
                skipped_lead += 1
                continue
            lead_id_val = ev.subject_id  # public_id of the lead
            try:
                lead = Lead.objects.get(public_id=lead_id_val)
            except Lead.DoesNotExist:
                continue
            from_val = ev.payload.get("from") or ""
            to_val = ev.payload.get("to") or ""
            if not to_val:
                continue
            with transaction.atomic():
                StageTransition.objects.create(
                    account=ev.account,
                    lead=lead,
                    from_value=from_val,
                    to_value=to_val,
                    occurred_at=ev.occurred_at,
                    actor=None,
                    owner_at_transition=None,
                    provenance=StageTransition.Provenance.BACKFILLED_EVENT,
                    source_event_id=src,
                )
            created_lead += 1

        # ---- 2. Backfill from deal.stage_changed events ----
        deal_events = Event.objects.filter(type="deal.stage_changed").select_related(
            "account"
        )
        created_deal = 0
        skipped_deal = 0
        for ev in deal_events:
            src = f"evt:{ev.public_id}"
            if StageTransition.objects.filter(source_event_id=src).exists():
                skipped_deal += 1
                continue
            deal_id_val = ev.subject_id
            try:
                deal = Deal.objects.get(public_id=deal_id_val)
            except Deal.DoesNotExist:
                continue
            from_val = ev.payload.get("from_stage") or ""
            to_val = ev.payload.get("to_stage") or ""
            if not to_val:
                continue
            with transaction.atomic():
                StageTransition.objects.create(
                    account=ev.account,
                    deal=deal,
                    from_value=from_val,
                    to_value=to_val,
                    occurred_at=ev.occurred_at,
                    actor=None,
                    owner_at_transition=None,
                    provenance=StageTransition.Provenance.BACKFILLED_EVENT,
                    source_event_id=src,
                )
            created_deal += 1

        # ---- 3. Estimated creation rows for leads with no creation transition ----
        estimated_lead = 0
        for lead in Lead.objects.all():
            if StageTransition.objects.filter(lead=lead, from_value="").exists():
                continue
            with transaction.atomic():
                StageTransition.objects.get_or_create(
                    lead=lead,
                    from_value="",
                    defaults=dict(
                        account=lead.account,
                        to_value=lead.status,
                        occurred_at=lead.created_at,
                        provenance=StageTransition.Provenance.ESTIMATED,
                    ),
                )
            estimated_lead += 1

        # ---- 4. Estimated creation rows for deals with no creation transition ----
        estimated_deal = 0
        for deal in Deal.objects.all():
            if StageTransition.objects.filter(deal=deal, from_value="").exists():
                continue
            stage_name = deal.stage.name if deal.stage_id else ""
            with transaction.atomic():
                StageTransition.objects.get_or_create(
                    deal=deal,
                    from_value="",
                    defaults=dict(
                        account=deal.account,
                        to_value=stage_name,
                        occurred_at=deal.created_at,
                        provenance=StageTransition.Provenance.ESTIMATED,
                    ),
                )
            estimated_deal += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. lead events: +{created_lead} skip={skipped_lead}; "
                f"deal events: +{created_deal} skip={skipped_deal}; "
                f"estimated lead creations: +{estimated_lead}; "
                f"estimated deal creations: +{estimated_deal}"
            )
        )
