"""Public API of instagram: the comment rules Instagram shares with other channels.

Moderation rules and comment triggers belong to the business, not to one channel:
each says which channels' comments it applies to (``RuleChannels``). Facebook Page
comments (``apps.facebook``) are matched and logged with these, through this module.
"""

from __future__ import annotations

from apps.instagram.models.moderation import ModerationLog, ModerationRule, RuleChannels
from apps.instagram.models.trigger import CommentTrigger
from apps.instagram.services.default_rules import add_default_moderation_rules
from apps.instagram.services.moderation import first_matching_rule
from apps.instagram.services.triggers import first_matching_trigger

__all__ = [
    "CommentTrigger",
    "ModerationLog",
    "ModerationRule",
    "RuleChannels",
    "add_default_moderation_rules",
    "first_matching_rule",
    "first_matching_trigger",
]
