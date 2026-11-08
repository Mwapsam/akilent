"""Apply the comment rules to comments that arrived before the rules did.

Rules only run on new comments, so spam left on posts from before a rule existed stays
visible. This runs the moderation rules over recent older comments **for moderation
only** (``moderate_comment(..., moderation_only=True)``):

* hide or flag, nothing else: no team email, no lead, no DM (comment triggers aren't run
  at all; Meta's 7-day private-reply limit would make late DMs fail or surprise people);
* a delete rule hides instead, so nothing old is removed for good;
* a comment that already has a moderation log is skipped, so it is safe to run twice.

``preview`` counts what would happen without touching anything; ``apply`` does it.
"""

from __future__ import annotations

import datetime
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

DAYS = 30
LIMIT = 2000  # comments per run; a busy page's month, kept to a bounded job


def _since(days: int):
    return timezone.now() - datetime.timedelta(days=days)


def instagram_candidates(account, days: int = DAYS):
    from apps.instagram.models.comment import Comment

    S = Comment.ModerationState
    return (
        Comment.objects.filter(
            thread__instagram_account__account=account,
            thread__instagram_account__is_active=True,
            direction=Comment.Direction.INBOUND,
            timestamp__gte=_since(days),
            moderation_logs__isnull=True,
        )
        .exclude(moderation_state__in=[S.HIDDEN, S.DELETED])
        .select_related("thread__instagram_account")
        .order_by("timestamp")
    )


def facebook_candidates(account, days: int = DAYS):
    from apps.facebook.models.comment import FacebookComment
    from apps.facebook.models.page import active_page

    conn = active_page(account)
    if conn is None:
        return FacebookComment.objects.none(), None
    S = FacebookComment.ModerationState
    qs = (
        FacebookComment.objects.filter(
            thread__account=account,
            thread__page_id=conn.page_id,
            direction=FacebookComment.Direction.INBOUND,
            timestamp__gte=_since(days),
            moderation_logs__isnull=True,
        )
        .exclude(moderation_state__in=[S.HIDDEN, S.DELETED])
        .select_related("thread")
        .order_by("timestamp")
    )
    return qs, conn


def _rules(account, channel: str) -> list:
    from apps.instagram.models.moderation import ModerationRule, RuleChannels

    return list(
        ModerationRule.objects.filter(
            account=account, is_active=True, channels__in=RuleChannels.covering(channel)
        ).order_by("priority", "created_at")
    )


def preview(account, days: int = DAYS) -> dict:
    """What "Apply rules to existing comments" would do, touching nothing."""
    from apps.instagram.models.moderation import ModerationRule
    from apps.instagram.services.moderation import backfill_action, body_matches_rule

    A = ModerationRule.ModerationAction
    counts = {"checked": 0, "hide": 0, "flag": 0, "days": days, "capped": False}
    fb_qs, _conn = facebook_candidates(account, days)
    for channel, qs in (
        ("instagram", instagram_candidates(account, days)),
        ("facebook", fb_qs),
    ):
        rules = _rules(account, channel)
        for body in qs.values_list("body", flat=True)[:LIMIT]:
            counts["checked"] += 1
            rule = next((r for r in rules if body_matches_rule(r, body or "")), None)
            action = backfill_action(rule.moderation_action) if rule else A.NONE
            if action == A.HIDE:
                counts["hide"] += 1
            elif action == A.FLAG:
                counts["flag"] += 1
        counts["capped"] = counts["capped"] or qs.count() > LIMIT
    return counts


def apply(account, days: int = DAYS) -> dict:
    """Run the rules (moderation only) over the older comments; the tally of outcomes."""
    from apps.facebook.services.moderation import moderate_comment as moderate_facebook
    from apps.instagram.models.moderation import ModerationLog
    from apps.instagram.services.moderation import (
        moderate_comment as moderate_instagram,
    )

    tally = {"checked": 0, "hidden": 0, "flagged": 0, "failed": 0}

    def count(log) -> None:
        if log is None:
            return
        if log.outcome == ModerationLog.Outcome.FAILED:
            tally["failed"] += 1
        elif log.outcome == ModerationLog.Outcome.APPLIED:
            tally["hidden" if log.moderation_action == "hide" else "flagged"] += 1

    for comment in list(instagram_candidates(account, days)[:LIMIT]):
        tally["checked"] += 1
        try:
            count(moderate_instagram(comment, moderation_only=True))
        except Exception:
            logger.exception("rules backfill: instagram comment %s failed", comment.pk)
            tally["failed"] += 1

    fb_qs, conn = facebook_candidates(account, days)
    for comment in list(fb_qs[:LIMIT]):
        tally["checked"] += 1
        try:
            count(moderate_facebook(comment, conn, moderation_only=True))
        except Exception:
            logger.exception("rules backfill: facebook comment %s failed", comment.pk)
            tally["failed"] += 1
    return tally


# ── Status for the page while the background job runs ───────────────────────


def status_key(account_id: int) -> str:
    return f"comment-rules-backfill:{account_id}"


def lock_key(account_id: int) -> str:
    return f"comment-rules-backfill-lock:{account_id}"
