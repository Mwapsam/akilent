"""A comment rule in one plain sentence, for the rules list (Stage 5 rule builder).

"When a comment on Instagram and Facebook mentions price or how much, reply by DM." The
sentence is built from the rule's own fields, so it can't drift from what the rule does.
"""

from __future__ import annotations

from apps.instagram.models import CommentTrigger, ModerationRule
from apps.instagram.models.moderation import RuleChannels


def _keywords(rule) -> list[str]:
    return [k.strip() for k in (rule.keywords or "").split(",") if k.strip()]


def _or_list(words: list[str], limit: int = 4) -> str:
    quoted = [f"“{w}”" for w in words[:limit]]
    if len(words) > limit:
        quoted.append(f"{len(words) - limit} more")
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + f" or {quoted[-1]}"


def _where(rule, show_channels: bool) -> str:
    if not show_channels:
        return "a comment"
    return {
        RuleChannels.INSTAGRAM: "a comment on Instagram",
        RuleChannels.FACEBOOK: "a comment on Facebook",
    }.get(rule.channels, "a comment on Instagram or Facebook")


_MODERATION_WHAT = {
    ModerationRule.MatchType.SPAM_DETECTION: "looks like spam",
    ModerationRule.MatchType.TOXICITY: "is abusive",
    ModerationRule.MatchType.BUYING_INTENT: "sounds ready to buy",
    ModerationRule.MatchType.COMPLAINT: "is a complaint",
    ModerationRule.MatchType.MENTION: "mentions your account",
}
_MODERATION_DO = {
    ModerationRule.ModerationAction.HIDE: "hide it",
    ModerationRule.ModerationAction.DELETE: "delete it",
    ModerationRule.ModerationAction.FLAG: "flag it for your team",
}
_AUTOMATION_DO = {
    ModerationRule.AutomationTrigger.NOTIFY_STAFF: "email your team",
    ModerationRule.AutomationTrigger.CREATE_PROPOSAL: "open a lead",
}


def sentence(rule, *, show_channels: bool = False) -> str:
    """The rule as one sentence, ending in a full stop."""
    where = _where(rule, show_channels)
    words = _keywords(rule)
    if isinstance(rule, CommentTrigger):
        if rule.match_type == CommentTrigger.MatchType.KEYWORD:
            when = f"When {where} mentions {_or_list(words)}" if words else f"When {where} matches"
        elif rule.match_type == CommentTrigger.MatchType.BUYING_INTENT:
            when = f"When {where} sounds ready to buy"
        else:
            when = f"When anyone leaves {where}"
        how = (
            "reply privately by DM"
            if rule.reply_type == CommentTrigger.ReplyType.DM
            else "reply publicly under it"
        )
        return f"{when}, {how}."

    if rule.match_type == ModerationRule.MatchType.KEYWORD:
        when = f"When {where} mentions {_or_list(words)}" if words else f"When {where} matches"
    else:
        when = f"When {where} {_MODERATION_WHAT.get(rule.match_type, 'matches')}"
        if words:
            when += f" (or mentions {_or_list(words)})"
    actions = [
        a
        for a in (
            _MODERATION_DO.get(rule.moderation_action),
            _AUTOMATION_DO.get(rule.automation_trigger),
        )
        if a
    ]
    does = " and ".join(actions) if actions else "do nothing yet"
    return f"{when}, {does}."
