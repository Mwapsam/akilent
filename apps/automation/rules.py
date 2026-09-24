import logging

from apps.automation.models import AutomationRule

logger = logging.getLogger(__name__)


def get_matching_rules(account_id: int, event: str) -> list[AutomationRule]:
    """Return active rules for the given account and trigger event."""
    return list(
        AutomationRule.objects.filter(
            account_id=account_id,
            trigger_event=event,
            is_active=True,
        )
    )


def evaluate_conditions(rule: AutomationRule, context: dict) -> bool:
    """
    Check whether all conditions in the rule match the event context.

    Conditions format example:
        {"message_contains": "hello", "contact_tag": "vip"}
    """
    conditions = rule.conditions
    if not conditions:
        return True

    for key, expected in conditions.items():
        actual = context.get(key)
        if actual is None:
            return False
        if isinstance(expected, str):
            if expected.lower() not in str(actual).lower():
                return False
        elif expected != actual:
            return False

    return True
