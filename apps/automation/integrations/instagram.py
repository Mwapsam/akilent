"""Reconciliation hook consumed by ``apps.instagram`` on permanent send failure.

Mirrors ``apps.automation.integrations.whatsapp`` for the Instagram channel.
``apps.instagram`` calls ``mark_outbound_message_failed(message)`` when an
``OutboundMessage`` reaches a terminal FAILED state; all automation-domain
logic lives here.
"""

import logging

logger = logging.getLogger(__name__)


def mark_outbound_message_failed(message) -> None:
    """Reconcile a permanently-failed Instagram ``OutboundMessage`` onto its step/run.

    Also cancels the open ``WorkflowInteraction`` for the step, so the interaction
    never silently blocks the contact from resuming a later workflow. Idempotent.
    """
    from apps.automation.interaction import cancel_open_interactions_for_run
    from apps.automation.models import WorkflowRun, WorkflowStepRun

    step_run = (
        WorkflowStepRun.objects.filter(ig_outbound_message_id=message.id)
        .exclude(status="failed")
        .select_related("run")
        .first()
    )
    if step_run is None:
        return

    step_run.status = "failed"
    step_run.result = {
        **step_run.result,
        "error": message.last_error or "Instagram send failed",
    }
    step_run.save(update_fields=["status", "result"])

    run = step_run.run
    if run.status not in (WorkflowRun.Status.FAILED, WorkflowRun.Status.CANCELLED):
        from django.db import transaction
        from django.utils import timezone

        with transaction.atomic():
            locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
            if locked_run.status not in (
                WorkflowRun.Status.FAILED,
                WorkflowRun.Status.CANCELLED,
            ):
                cancel_open_interactions_for_run(locked_run)
                locked_run.status = WorkflowRun.Status.FAILED
                locked_run.completed_at = timezone.now()
                locked_run.save(update_fields=["status", "completed_at"])
                logger.info(
                    "mark_outbound_message_failed (IG): workflow run %s failed via outbound message %s",
                    locked_run.pk,
                    message.id,
                )
