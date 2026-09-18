"""Public API for the automation module.

This is the single interface for rule evaluation and execution.
The implementation of these functions can change internally without affecting call sites.

Direct imports of apps.automation.* are not allowed outside of automation.
Direct imports of automation rules/workflows should go through this API.
"""
import logging
from typing import Optional

from apps.accounts.models import Account

logger = logging.getLogger(__name__)


def get_matching_rules(account: Account, trigger_event: str):
    """Get AutomationRule instances matching a trigger event for an account.

    Args:
        account: The account to query rules for
        trigger_event: The trigger event type (e.g. 'message_received')

    Returns:
        QuerySet of matching AutomationRule instances
    """
    from apps.automation.models import AutomationRule

    return AutomationRule.objects.filter(
        account=account,
        trigger_event=trigger_event,
        is_active=True,
    )


def count_active_rules(account: Account) -> int:
    """Count all active automation rules for an account.

    Used by billing to enforce plan limits.

    Args:
        account: The account to query rules for

    Returns:
        Count of active rules
    """
    from apps.automation.models import AutomationRule

    return AutomationRule.objects.filter(
        account=account,
        is_active=True,
    ).count()


def evaluate_conditions(rule, context: dict) -> bool:
    """Evaluate whether a rule's conditions match the given context.

    Args:
        rule: An AutomationRule instance
        context: Context dict with message/event data

    Returns:
        True if all conditions are met, False otherwise
    """
    from apps.automation.rules import evaluate_conditions as _evaluate

    return _evaluate(rule, context)


def execute_rule(rule, context: dict) -> dict:
    """Execute a rule's actions for the given context.

    This is called after conditions have been verified. The rule's actions
    are executed, and the result is returned. If execution fails, an exception
    is logged and re-raised (caller responsibility to handle).

    Args:
        rule: An AutomationRule instance
        context: Context dict with message/event data

    Returns:
        dict with execution result info (implementation-dependent)

    Raises:
        Any exception raised by the rule action execution
    """
    from apps.automation.workflows import execute_rule as _execute

    return _execute(rule, context)


def upsert_published_workflow(account, *, slug: str, name: str, definition: dict):
    """Create or update a published Workflow by slug — idempotent by design.

    Used by ``apps.verticals`` to install a vertical Starter pack's bundled
    workflows without importing ``apps.automation.models`` directly (module
    boundary rule: cross-app model access goes through this public API).
    """
    from apps.automation.models import Workflow

    workflow, _ = Workflow.objects.update_or_create(
        account=account, slug=slug,
        defaults={"name": name, "definition": definition, "status": Workflow.Status.PUBLISHED},
    )
    return workflow


# Re-export for Phase 2 compatibility (triggers still imports from here internally)
# This will be removed once triggers.py is fully migrated to use dispatcher
from apps.automation.triggers import (  # noqa: E402, F401
    on_message_received,
    on_message_sent,
)
