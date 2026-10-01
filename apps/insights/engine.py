"""Insight generation engine.

upsert_insight() saves (or refreshes) an insight without creating duplicates.
run_all_rules() drives the full generation cycle for a single account.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def upsert_insight(insight_instance) -> tuple:
    """Save *insight_instance* (unsaved model object) respecting the uniqueness constraint.

    - If no open insight of the same type exists for the account, create it.
    - If one exists, update its evidence, count, body and severity (the facts may have changed).
    - Returns (insight, created: bool).
    """
    from apps.insights.models import Insight

    existing = Insight.objects.filter(
        account=insight_instance.account,
        type=insight_instance.type,
        status__in=[Insight.Status.NEW, Insight.Status.ACKNOWLEDGED],
    ).first()

    if existing:
        existing.title = insight_instance.title
        existing.body = insight_instance.body
        existing.severity = insight_instance.severity
        existing.evidence = insight_instance.evidence
        existing.evidence_count = insight_instance.evidence_count
        existing.suggested_action = insight_instance.suggested_action
        existing.save(
            update_fields=[
                "title",
                "body",
                "severity",
                "evidence",
                "evidence_count",
                "suggested_action",
                "updated_at",
            ]
        )
        return existing, False

    insight_instance.save()
    return insight_instance, True


def run_all_rules(account) -> dict:
    """Run every registered rule for *account* and upsert the results.

    Returns a summary dict: {"created": n, "updated": n, "skipped": n, "errors": n}
    """
    from apps.insights.rules import RULES

    summary = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    for rule in RULES:
        try:
            insight = rule(account)
        except Exception:
            logger.exception(
                "run_all_rules: rule %s failed for account %s",
                rule.__name__,
                account.pk,
            )
            summary["errors"] += 1
            continue

        if insight is None:
            summary["skipped"] += 1
            continue

        try:
            _, created = upsert_insight(insight)
            if created:
                summary["created"] += 1
            else:
                summary["updated"] += 1
        except Exception:
            logger.exception(
                "run_all_rules: upsert failed for type=%s account=%s",
                insight.type,
                account.pk,
            )
            summary["errors"] += 1

    return summary
