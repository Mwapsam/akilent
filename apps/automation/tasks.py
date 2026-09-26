"""
Celery tasks for automation rule evaluation.

These tasks are dispatched asynchronously from domain event subscribers,
keeping the event publisher (WhatsApp webhook handling) decoupled from
the automation engine's latency.
"""
import json
import logging

import requests
from celery import shared_task

from apps.automation.rules import evaluate_conditions, get_matching_rules
from apps.automation.workflows import execute_rule

logger = logging.getLogger(__name__)

_WEBHOOK_MAX_RETRIES = 6
_WEBHOOK_RETRY_DELAY_BASE = 10  # seconds
_WEBHOOK_RETRY_DELAY_MULTIPLIER = 2
_WEBHOOK_TIMEOUT_SECONDS = 10


def _webhook_backoff_delay(retry_count: int) -> int:
    return _WEBHOOK_RETRY_DELAY_BASE * (_WEBHOOK_RETRY_DELAY_MULTIPLIER ** retry_count)


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
def find_repeated_replies() -> int:
    """Daily: for each business, the replies its team keeps sending by hand (``patterns.refresh``).
    Returns how many businesses have at least one."""
    from apps.accounts.api import list_active_accounts
    from apps.automation import patterns

    with_patterns = 0
    for account in list_active_accounts():
        try:
            with_patterns += bool(patterns.refresh(account))
        except Exception:  # noqa: BLE001 - one business's data must not stop the others
            logger.exception("find_repeated_replies failed for account=%s", account.pk)
    return with_patterns


@shared_task(queue="celery")
def run_due_workflows() -> int:
    """Resume lifecycle WorkflowRuns whose wait timers have elapsed."""
    from apps.automation.workflow_engine import run_due

    n = run_due()
    if n:
        logger.info("run_due_workflows: advanced %d run(s)", n)
    return n


@shared_task(bind=True, max_retries=_WEBHOOK_MAX_RETRIES, queue="webhooks")
def deliver_workflow_webhook(self, delivery_id: int) -> None:
    """POST a Workflow ``webhook`` step's payload, with exponential backoff.

    Mirrors apps.email.tasks.deliver_webhook's retry shape, but the target URL
    is business-supplied per step (not a pre-registered WebhookEndpoint), so
    every attempt is SSRF-guarded via the same check used for template
    data-source fetches.
    """
    from apps.automation.models import WorkflowWebhookDelivery
    from apps.email.services.datafetch import DataFetchError, _assert_public_https

    try:
        delivery = WorkflowWebhookDelivery.objects.get(pk=delivery_id)
    except WorkflowWebhookDelivery.DoesNotExist:
        logger.error("deliver_workflow_webhook: delivery %s not found", delivery_id)
        return

    if delivery.status == WorkflowWebhookDelivery.Status.SUCCEEDED:
        return

    try:
        _assert_public_https(delivery.url)
    except DataFetchError as exc:
        # Not retryable — a business-supplied URL that resolves to a private
        # target will never become valid on retry.
        delivery.mark_failed(None, str(exc), exhausted=True)
        logger.warning("deliver_workflow_webhook: SSRF guard rejected delivery %s: %s", delivery_id, exc)
        return

    try:
        response = requests.request(
            delivery.method,
            delivery.url,
            headers=delivery.headers or {},
            data=json.dumps(delivery.body).encode(),
            timeout=_WEBHOOK_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        response.raise_for_status()
        delivery.mark_succeeded(response.status_code)
    except Exception as exc:  # noqa: BLE001
        response_obj = getattr(exc, "response", None)
        response_code = response_obj.status_code if response_obj is not None else None

        is_last = self.request.retries >= _WEBHOOK_MAX_RETRIES
        delivery.mark_failed(response_code, str(exc), exhausted=is_last)
        if is_last:
            logger.error(
                "deliver_workflow_webhook: exhausted retries for delivery %s (%s): %s",
                delivery_id, delivery.url, exc,
            )
            return
        raise self.retry(exc=exc, countdown=_webhook_backoff_delay(self.request.retries))
