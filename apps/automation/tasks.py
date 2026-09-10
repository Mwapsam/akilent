"""
Celery tasks for automation rule evaluation.

These tasks are dispatched asynchronously from domain event subscribers,
keeping the event publisher (WhatsApp webhook handling) decoupled from
the automation engine's latency.
"""
import logging

from celery import shared_task

from apps.automation.rules import evaluate_conditions, get_matching_rules
from apps.automation.workflows import execute_rule

logger = logging.getLogger(__name__)


@shared_task
def evaluate_rules_for_message(account_id: int, trigger_event: str, context: dict) -> None:
    """Evaluate and execute automation rules for a trigger event.

    This is dispatched asynchronously onto the 'automation' Celery queue
    from domain event subscribers, so rule execution doesn't block the
    event publisher.

    Args:
        account_id: The Akilent tenant (Account) ID.
        trigger_event: The automation rule trigger (e.g. MESSAGE_RECEIVED).
        context: Event context dict with fields like phone_number, message_type, etc.
    """
    matching_rules = get_matching_rules(account_id, trigger_event)

    for rule in matching_rules:
        if evaluate_conditions(rule, context):
            try:
                execute_rule(rule, context)
            except Exception:
                logger.exception("evaluate_rules_for_message: error executing rule pk=%s", rule.pk)
                # Don't re-raise — continue to next rule if one fails


@shared_task(queue="celery")
def run_due_workflows() -> int:
    """Resume lifecycle WorkflowRuns whose wait timers have elapsed."""
    from apps.automation.workflow_engine import run_due

    n = run_due()
    if n:
        logger.info("run_due_workflows: advanced %d run(s)", n)
    return n
