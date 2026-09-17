"""Reconciliation hook consumed by ``apps.whatsapp`` on permanent send failure.

Keeps the dependency direction one-way: ``apps.whatsapp`` knows nothing about
workflows — it just calls ``mark_outbound_message_failed(message)`` after an
``OutboundMessage`` reaches its terminal ``FAILED`` state. All automation-domain
logic (looking up the WorkflowStepRun, failing the parent WorkflowRun) lives
here, on the automation side of the boundary, so this module can be extended
the same way for future channels (email/SMS/voice) without ``apps.whatsapp``
(or any other channel app) ever importing automation models.
"""
import logging

logger = logging.getLogger(__name__)


def mark_outbound_message_failed(message) -> None:
    """Reconcile a permanently-failed ``OutboundMessage`` onto its workflow step/run.

    Idempotent: safe to call more than once for the same message (e.g. a
    retried task) — a step/run already marked failed is left untouched.
    """
    from apps.automation.models import WorkflowRun, WorkflowStepRun

    step_run = (
        WorkflowStepRun.objects.filter(outbound_message_id=message.id)
        .exclude(status="failed")
        .select_related("run")
        .first()
    )
    if step_run is None:
        return

    step_run.status = "failed"
    step_run.result = {**step_run.result, "error": message.last_error or "WhatsApp send failed"}
    step_run.save(update_fields=["status", "result"])

    # The run may already show COMPLETED — the step recorded "ok" and the workflow
    # advanced before Meta's permanent rejection arrived asynchronously. That's
    # exactly the "false success" bug this hook exists to correct, so COMPLETED
    # is not protected here; only an already-reconciled or already-terminal run is.
    run = step_run.run
    if run.status not in (WorkflowRun.Status.FAILED, WorkflowRun.Status.CANCELLED):
        from django.utils import timezone

        run.status = WorkflowRun.Status.FAILED
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "completed_at"])
        logger.info(
            "mark_outbound_message_failed: workflow run %s failed via outbound message %s",
            run.pk, message.id,
        )
