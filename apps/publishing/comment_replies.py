"""A post's own comment auto-reply (outreach plan Phase C, flag ``cs_comment_dm_link``).

On a post's page the business says "when someone comments *price*, send them a private
message", with the post's WhatsApp chat link in it. That is an ordinary comment reply rule
(``apps.instagram.models.CommentTrigger``) with ``post`` set: it only answers comments on
this post, and comes before the general rules there (``triggers.first_matching_trigger``).
Firing, idempotency and crediting the DM to the post are the rule engine's, unchanged.

A chat the customer then opens from ``{whatsapp_link}`` carries the post's ref code, so it
is credited to the post like any other chat from the link.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FLAG = "cs_comment_dm_link"
COMMENT_CHANNELS = ("instagram", "facebook")
# Whole words (triggers.keyword_in). "pp" (price please), "hm" (how much), "dm" and
# "inbox" are how many buyers ask under a post.
DEFAULT_KEYWORDS = "price, how much, cost, interested, pp, hm, dm, inbox, available"
DEFAULT_REPLY = (
    "Hi {username}! Thanks for asking. Chat with us on WhatsApp and we'll send you "
    "the details: {whatsapp_link}"
)
REPLY_MAX = 1000


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def channels_for(post, account) -> list[str]:
    """The post's comment channels this business can write rules for, in a stable order."""
    from apps.instagram.rules_views import rule_channels

    allowed = set(rule_channels(account))
    return [c for c in COMMENT_CHANNELS if c in (post.platforms or []) and c in allowed]


def rules_for(post):
    from apps.instagram.models import CommentTrigger

    return CommentTrigger.objects.filter(post=post).order_by("created_at")


def state(post, account) -> dict | None:
    """What the post page's "Auto-reply to comments" card shows; None when it doesn't
    apply (not released, or the post isn't on a channel this business can reply on)."""
    if not is_released():
        return None
    channels = channels_for(post, account)
    if not channels:
        return None
    from . import links

    rules = list(rules_for(post))
    has_link = bool(links.links_for_post(post))
    return {
        "channels": channels,
        "rules": rules,
        "has_link": has_link,
        "default_keywords": DEFAULT_KEYWORDS,
        "default_reply": DEFAULT_REPLY,
    }


@dataclass
class SaveResult:
    rule: object | None = None
    errors: dict[str, str] = field(default_factory=dict)
    # Said after saving: e.g. no WhatsApp number, so the link can't be made yet.
    note: str = ""


def save(post, account, data, rule=None) -> SaveResult:
    """Create (or update ``rule``) from the card's form. Validates like the rules page."""
    from apps.instagram.models import CommentTrigger
    from apps.instagram.models.moderation import RuleChannels

    result = SaveResult()
    channels = channels_for(post, account)
    if not channels:
        result.errors["__all__"] = "This post isn't on a channel you can reply on."
        return result

    every = bool(data.get("every_comment"))
    keywords = ", ".join(
        k.strip() for k in (data.get("keywords") or "").split(",") if k.strip()
    )
    reply = (data.get("reply_template") or "").strip()
    if not every and not keywords:
        result.errors["keywords"] = "Add at least one word, or choose every comment."
    if not reply:
        result.errors["reply_template"] = "Write the message they'll get."
    elif len(reply) > REPLY_MAX:
        result.errors["reply_template"] = (
            f"Keep the message under {REPLY_MAX} characters."
        )
    if result.errors:
        return result

    from .templatetags.publishing_filters import post_headline

    rule = rule or CommentTrigger(account=account, post=post)
    rule.name = f"Comments on “{post_headline(post)}”"[:100]
    rule.channels = channels[0] if len(channels) == 1 else RuleChannels.BOTH
    rule.match_type = (
        CommentTrigger.MatchType.ANY_COMMENT
        if every
        else CommentTrigger.MatchType.KEYWORD
    )
    rule.keywords = "" if every else keywords
    rule.reply_template = reply
    rule.reply_type = CommentTrigger.ReplyType.DM
    # A new rule starts on; editing one keeps whether it's paused.
    rule.save()
    result.rule = rule
    if "{whatsapp_link}" in reply:
        result.note = ensure_links(post)
    return result


def ensure_links(post) -> str:
    """Give each of the post's comment channels a chat link if it has none at all, so
    ``{whatsapp_link}`` has something to send. One the business switched off stays off.
    Returns what to tell them when a link can't be made ("" when all is well)."""
    from . import links
    from .models import ContentPostVariant, TrackedLink

    variants = post.variants.filter(platform__in=COMMENT_CHANNELS).exclude(
        status=ContentPostVariant.Status.CANCELLED
    )
    missing = [
        v for v in variants if not TrackedLink.objects.filter(variant=v).exists()
    ]
    try:
        for variant in missing:
            links.link_for_variant(variant)
    except links.NoWhatsAppNumber:
        return (
            "Saved. Connect a WhatsApp number to add the chat link: until then the "
            "message goes out without it."
        )
    if any(links.active_link(v) is None for v in variants):
        return (
            "Saved. This post's chat link is switched off on a channel, so that "
            "channel's message goes out without it."
        )
    return ""
