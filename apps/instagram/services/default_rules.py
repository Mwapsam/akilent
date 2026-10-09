"""Recommended moderation rules every Instagram business starts with.

They are ordinary ModerationRule rows, so the business edits, pauses or deletes
them like any rule it wrote. Hiding is kept to spam; abuse and complaints are
flagged (the team is emailed) because the heuristics can misread a compliment
("to die for") or a customer who needs an answer, not hiding.
"""

from __future__ import annotations

from apps.instagram.models.moderation import ModerationRule

SPAM_KEYWORDS = (
    "DM me, message me, contact me, whatsapp me, telegram me, earn money, "
    "make money, easy money, free money, investment opportunity, crypto, bitcoin, "
    "forex, double your money, guaranteed profit, congratulations you won, "
    "claim your prize"
)

DEFAULT_MODERATION_RULES: list[dict] = [
    {
        "name": "Spam and scams",
        "match_type": ModerationRule.MatchType.SPAM_DETECTION,
        "keywords": SPAM_KEYWORDS,
        "moderation_action": ModerationRule.ModerationAction.HIDE,
        "automation_trigger": ModerationRule.AutomationTrigger.NONE,
        "priority": 10,
    },
    {
        "name": "Abusive language",
        "match_type": ModerationRule.MatchType.TOXICITY,
        "keywords": "",
        "moderation_action": ModerationRule.ModerationAction.FLAG,
        "automation_trigger": ModerationRule.AutomationTrigger.NONE,
        "priority": 20,
    },
    {
        "name": "Complaints",
        "match_type": ModerationRule.MatchType.COMPLAINT,
        "keywords": "",
        "moderation_action": ModerationRule.ModerationAction.FLAG,
        "automation_trigger": ModerationRule.AutomationTrigger.NONE,
        "priority": 30,
    },
]


def missing_default_rules(account) -> list[dict]:
    """The recommended rules the account doesn't have (matched by name)."""
    existing = {
        name.lower()
        for name in ModerationRule.objects.filter(account=account).values_list(
            "name", flat=True
        )
    }
    return [r for r in DEFAULT_MODERATION_RULES if r["name"].lower() not in existing]


def add_default_moderation_rules(account, *, only_if_none: bool = False) -> int:
    """Create the recommended rules the account is missing. Returns how many.

    ``only_if_none`` is for connecting an account: a business that already set
    up moderation is left as it is.
    """
    if only_if_none and ModerationRule.objects.filter(account=account).exists():
        return 0
    missing = missing_default_rules(account)
    ModerationRule.objects.bulk_create(
        ModerationRule(account=account, **rule) for rule in missing
    )
    return len(missing)
