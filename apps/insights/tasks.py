"""Celery tasks for the insights app.

generate_insights  — runs all insight rules for every active account (nightly).
evaluate_policies  — evaluates all active BusinessPolicy rows (every 15 minutes).

Both tasks iterate all active accounts in a single worker invocation.  If an
account raises an unexpected error the exception is logged and the task continues
with the next account — one bad account does not abort the entire sweep.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="scheduler", name="apps.insights.tasks.generate_insights")
def generate_insights() -> None:
    """Run all insight rules for every active account and upsert results."""
    from apps.accounts.models import Account
    from apps.insights.engine import run_all_rules

    for account in Account.objects.filter(is_active=True).iterator():
        try:
            run_all_rules(account)
        except Exception:
            logger.exception("generate_insights: failed for account=%s", account.pk)


@shared_task(queue="scheduler", name="apps.insights.tasks.evaluate_policies")
def evaluate_policies() -> None:
    """Evaluate and execute all active BusinessPolicy rows for every active account."""
    from apps.accounts.models import Account
    from apps.insights.executor import evaluate_policies as _evaluate

    for account in Account.objects.filter(is_active=True).iterator():
        try:
            _evaluate(account)
        except Exception:
            logger.exception("evaluate_policies: failed for account=%s", account.pk)
