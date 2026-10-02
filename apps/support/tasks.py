import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def evaluate_sla():
    """Scan all open tickets; mark breached; trigger escalation for at-risk."""
    from apps.support.services.sla import evaluate_open_tickets

    at_risk, breached = evaluate_open_tickets()
    if at_risk or breached:
        logger.info("evaluate_sla: at_risk=%s breached=%s", at_risk, breached)
