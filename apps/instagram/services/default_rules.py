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


def _rules_for(account, channel: str | None):
    from apps.instagram.models.moderation import RuleChannels

    rules = ModerationRule.objects.filter(account=account)
    if channel:
        rules = rules.filter(channels__in=RuleChannels.covering(channel))
    return rules


def missing_default_rules(account, channel: str | None = None) -> list[dict]:
    """The recommended rules the account doesn't have (matched by name).

    With ``channel`` ("instagram" or "facebook"), only rules covering that channel count.
    """
    existing = {
        name.lower()
        for name in _rules_for(account, channel).values_list("name", flat=True)
    }
    return [r for r in DEFAULT_MODERATION_RULES if r["name"].lower() not in existing]


def add_default_moderation_rules(
    account, *, only_if_none: bool = False, channel: str | None = None
) -> int:
    """Create the recommended rules the account is missing. Returns how many.

    ``only_if_none`` is for connecting an account: a business that already set
    up moderation is left as it is. ``channel`` limits both to that channel's rules,
    and the new rules apply to it alone; without it they apply to both channels.
    """
    from apps.instagram.models.moderation import RuleChannels

    if only_if_none and _rules_for(account, channel).exists():
        return 0
    missing = missing_default_rules(account, channel)
    ModerationRule.objects.bulk_create(
        ModerationRule(account=account, channels=channel or RuleChannels.BOTH, **rule)
        for rule in missing
    )
    return len(missing)
